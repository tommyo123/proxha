import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA = '''
CREATE TABLE IF NOT EXISTS notification_settings (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    url BLOB, events TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notification_state (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(connection_id,key)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    event TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
    delivered_at REAL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, error TEXT
);
'''
EVENTS = ('migration_failed', 'unresolved_outcome', 'rule_violated', 'queue_failed')
LABELS = {'migration_failed': 'Migration failed', 'unresolved_outcome': 'Migration outcome needs review',
          'rule_violated': 'Placement rule violated', 'queue_failed': 'Migration queue stopped', 'test': 'Test notification'}
EXPIRY = 86400
INTERVAL = 15


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    # None keeps the saved URL; an empty string removes it.
    url: str | None = Field(default=None, max_length=2000)
    events: list[Literal['migration_failed', 'unresolved_outcome', 'rule_violated', 'queue_failed']] = Field(default_factory=lambda: list(EVENTS), max_length=4)
    revision: int = Field(ge=0, strict=True)

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        if value:
            parts = urlsplit(value.strip())
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                raise ValueError('Enter an http or https webhook URL.')
            return value.strip()
        return value


def row(db, connection_id):
    return db.execute('SELECT * FROM notification_settings WHERE connection_id=?', (connection_id,)).fetchone()


def read(db, connection_id, cipher):
    saved = row(db, connection_id)
    target = webhook(saved, cipher)
    recent = [dict(r) for r in db.execute('''SELECT id,event,message,created_at,delivered_at,attempts,error FROM notifications
        WHERE connection_id=? ORDER BY id DESC LIMIT 10''', (connection_id,))]
    # The URL can carry a token, so only its host is shown after saving.
    return {'enabled': bool(saved and saved['enabled']), 'events': json.loads(saved['events']) if saved else list(EVENTS),
            'revision': saved['revision'] if saved else 0, 'url_configured': bool(target),
            'url_host': urlsplit(target).hostname if target else None, 'recent': recent}


def webhook(saved, cipher):
    return cipher.decrypt(saved['url']).decode() if saved and saved['url'] else None


def save(db, connection_id, data, cipher):
    saved = row(db, connection_id)
    if (saved['revision'] if saved else 0) != data.revision:
        raise HTTPException(409, 'Notification settings changed in another session. Reload them.')
    url = saved['url'] if saved and data.url is None else cipher.encrypt(data.url.encode()) if data.url else None
    if data.enabled and not url:
        raise HTTPException(422, 'Enter a webhook URL before enabling notifications.')
    db.execute('INSERT OR REPLACE INTO notification_settings VALUES(?,?,?,?,?)',
               (connection_id, url, json.dumps(data.events), int(data.enabled), data.revision + 1))
    return read(db, connection_id, cipher)


def queue(db, connection_id, event, message, now):
    db.execute('INSERT INTO notifications(connection_id,event,message,created_at,next_at) VALUES(?,?,?,?,?)',
               (connection_id, event, message, now, now))


def collect(db, connection_id, now):
    """Queue events that appeared since the last check. Disabled settings only advance the cursors."""
    from . import balancing, placement_rules
    saved = row(db, connection_id)
    wanted = set(json.loads(saved['events'])) if saved and saved['enabled'] else set()
    state = {r['key']: json.loads(r['value']) for r in db.execute('SELECT key,value FROM notification_state WHERE connection_id=?', (connection_id,))}
    found = []
    since = state.get('failures_since', now)
    failures = db.execute('SELECT resource,source,target,message,occurred_at FROM migration_failures WHERE connection_id=? AND occurred_at>? ORDER BY occurred_at',
                          (connection_id, since)).fetchall()
    found += [('migration_failed', f'{r["resource"]}: {r["source"]} → {r["target"]} failed. {r["message"]}') for r in failures]
    since = max([since, *(r['occurred_at'] for r in failures)])
    unknown = {str(r['id']): r for r in db.execute('''SELECT t.id,t.vmid,t.name,t.message FROM operation_tasks t JOIN operations o ON o.id=t.operation_id
        WHERE o.connection_id=? AND t.status='unknown' ''', (connection_id,))}
    found += [('unresolved_outcome', f'{r["vmid"]} {r["name"] or ""}: {r["message"]} Check Proxmox and acknowledge the outcome in ProxHA.'.replace('  ', ' '))
              for key, r in unknown.items() if key not in state.get('unknown', [])]
    runs_since = state.get('runs_since', now)
    runs = db.execute("SELECT trigger,status,message,finished_at FROM migration_runs WHERE connection_id=? AND status IN ('failed','interrupted') AND finished_at>? ORDER BY finished_at",
                      (connection_id, runs_since)).fetchall()
    found += [('queue_failed', f'{r["trigger"].replace("_", " ").capitalize()} queue {r["status"]}: {r["message"]}') for r in runs]
    runs_since = max([runs_since, *(r['finished_at'] for r in runs)])
    evaluation = balancing.latest(db, connection_id) or {}
    names = {str(r['id']): r['name'] for r in placement_rules.list_rules(db, connection_id)}
    violated = {str(s['id']): s for s in evaluation.get('placement_rules') or [] if s.get('violations') and s.get('enforcement', 'should') != 'report'}
    found += [('rule_violated', f'Rule "{names.get(key, s.get("name") or key)}" has {s["violations"]} placement conflicts ({s.get("enforcement", "should")}).')
              for key, s in violated.items() if key not in state.get('violated', [])]
    for event, message in found:
        if event in wanted:
            queue(db, connection_id, event, message, now)
    for key, value in (('failures_since', since), ('unknown', sorted(unknown)), ('runs_since', runs_since), ('violated', sorted(violated))):
        db.execute('INSERT OR REPLACE INTO notification_state VALUES(?,?,?)', (connection_id, key, json.dumps(value)))
    db.execute('DELETE FROM notifications WHERE connection_id=? AND created_at<?', (connection_id, now - 7 * 86400))


def payload(name, item):
    title = f'ProxHA {name}: {LABELS.get(item["event"], item["event"])}'
    # "text" lets chat webhooks such as Slack, Mattermost and Teams workflows display the message directly.
    return {'event': item['event'], 'connection': name, 'title': title, 'message': item['message'],
            'text': f'{title}\n{item["message"]}', 'created_at': datetime.fromtimestamp(item['created_at'], timezone.utc).isoformat()}


async def deliver(database, connection_id, cipher, client, now, only=None):
    with database() as db:
        saved = row(db, connection_id)
        target = webhook(saved, cipher)
        name = (db.execute('SELECT name FROM connections WHERE id=?', (connection_id,)).fetchone() or {'name': str(connection_id)})['name']
        if only:
            clause, params = 'id=?', (only,)
        else:
            # Only subscribed events are selected, so unsendable items cannot fill a batch.
            # Test items are sent by the test endpoint only.
            events = [e for e in json.loads(saved['events']) if e in EVENTS] if saved and saved['enabled'] else []
            clause = f'delivered_at IS NULL AND next_at<=? AND created_at>=? AND event IN ({",".join("?" * len(events))})'
            params = (now, now - EXPIRY, *events)
        items = [dict(r) for r in db.execute(f'SELECT * FROM notifications WHERE connection_id=? AND {clause} ORDER BY id LIMIT 20', (connection_id, *params))] if only or events else []
    if not target or not items:
        return []
    results = []
    for item in items:
        with database() as db:
            current = row(db, connection_id)
            # Changed settings end the batch; the next run selects items with the new settings.
            if not current or current['revision'] != saved['revision']:
                break
        error = None
        try:
            response = await client.post(target, json=payload(name, item))
            if response.status_code >= 300:
                error = f'Webhook returned HTTP {response.status_code}.'
        except httpx.HTTPError as exc:
            error = f'Webhook request failed: {type(exc).__name__}.'
        with database() as db:
            if error:
                db.execute('UPDATE notifications SET attempts=attempts+1,next_at=?,error=? WHERE id=?',
                           (now + min(3600, 30 * 2 ** item['attempts']), error, item['id']))
            else:
                db.execute('UPDATE notifications SET attempts=attempts+1,delivered_at=?,error=NULL WHERE id=?', (now, item['id']))
        results.append({'id': item['id'], 'delivered': error is None, 'error': error})
    return results


class Worker:
    def __init__(self, database, cipher, client_factory=lambda: httpx.AsyncClient(timeout=10, follow_redirects=False)):
        self.database, self.cipher, self.client_factory = database, cipher, client_factory
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name='notifications')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def tick(self, now=None):
        now = time.time() if now is None else now
        with self.database() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
            for connection_id in ids:
                collect(db, connection_id, now)
        async with self.client_factory() as client:
            for connection_id in ids:
                await deliver(self.database, connection_id, self.cipher(), client, now)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).exception('Notification delivery failed; retrying.')
            await asyncio.sleep(INTERVAL)
