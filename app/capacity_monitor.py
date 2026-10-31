import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import background, capacity, native_ha, node_maintenance
from .proxmox import ProxmoxClient

SCHEMA = '''
CREATE TABLE IF NOT EXISTS capacity_settings (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    config TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_state (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    checked_at REAL NOT NULL, next_at REAL NOT NULL, revision INTEGER NOT NULL,
    report TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS capacity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    occurred_at REAL NOT NULL, summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capacity_events_connection_time ON capacity_events(connection_id,occurred_at);
'''
RETENTION = 7 * 86400
POLL_SECONDS = 30
COLLECT_SECONDS = 45
CALCULATION_SECONDS = 15


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=True, strict=True)
    show_cluster_warning: bool = Field(default=False, strict=True)
    memory_mode: Literal['configured', 'observed'] = 'configured'
    interval_seconds: Literal[300, 600, 900] = 300
    revision: int = Field(default=0, ge=0, strict=True)


def settings(db, connection_id):
    row = db.execute('SELECT * FROM capacity_settings WHERE connection_id=?', (connection_id,)).fetchone()
    defaults = SettingsInput().model_dump()
    return {**defaults, **json.loads(row['config']), 'revision': row['revision']} if row else defaults


def save(db, connection_id, value):
    if settings(db, connection_id)['revision'] != value.revision:
        raise HTTPException(409, 'Capacity settings changed in another session. Reload before saving.')
    db.execute('INSERT OR REPLACE INTO capacity_settings VALUES(?,?,?)',
               (connection_id, value.model_dump_json(exclude={'revision'}), value.revision + 1))
    db.execute('DELETE FROM capacity_state WHERE connection_id=?', (connection_id,))


def summarize(report, error=None, stale=False):
    if stale or error or not report:
        return {'status': 'unknown', 'message': error or ('The last capacity check is stale.' if stale else 'Waiting for the first capacity check.'),
                'failed_hosts': [], 'exception_hosts': [], 'overcommitted_hosts': [], 'uncertain_hosts': [], 'unplaced_guests': []}
    scenarios = report['scenarios']
    result = capacity.classify(scenarios, report.get('unassessed_guests'))
    unplaced = sorted({g['resource'] for s in scenarios for g in s['unplaced']})
    # Scenarios that fail only because a surviving host is already overcommitted name that host instead.
    short = sorted(s['failed_host'] for s in scenarios if s['status'] == 'insufficient' and (s['unplaced'] or not s.get('overcommitted_hosts')))
    parts = []
    if result['overcommitted_hosts']:
        parts.append('Hosts already exceed the reserved RAM/CPU capacity before any failure: ' + ', '.join(result['overcommitted_hosts']) + '.')
    if short:
        parts.append('No complete placement within the capacity and placement limits after loss of: ' + ', '.join(short) + '.')
    if result['exception_hosts']:
        parts.append('Placement only with Must rule exceptions after loss of: ' + ', '.join(result['exception_hosts']) + '.')
    if parts and result['incomplete']:
        parts.append('Other scenarios or guests also need verification.')
    message = ' '.join(parts) or (
        'Capacity is not established: incomplete measurements or a bounded search without a complete placement.' if result['incomplete'] else
        'All assessed single-host failures have a capacity placement.')
    return {'status': result['status'], 'message': message, 'failed_hosts': result['failed_hosts'],
            'exception_hosts': result['exception_hosts'], 'overcommitted_hosts': result['overcommitted_hosts'],
            'uncertain_hosts': result['uncertain_hosts'], 'unplaced_guests': unplaced}


def check_seconds(hosts):
    """Longest expected check: inventory reads plus a bounded search for each simulated failure."""
    return COLLECT_SECONDS + CALCULATION_SECONDS + 2 * capacity.SEARCH_SECONDS * hosts


def stale_after(db, connection_id, interval):
    """Result age at which a check is overdue, given that the worker checks connections in sequence."""
    own, others = check_seconds(0), 0
    for row in db.execute("SELECT c.id,COALESCE(json_array_length(s.report,'$.scenarios'),0) AS hosts "
                          'FROM connections c LEFT JOIN capacity_state s ON s.connection_id=c.id'):
        if row['id'] == connection_id:
            own = check_seconds(row['hosts'])
        elif settings(db, row['id'])['enabled']:
            others += check_seconds(row['hosts'])
    # A due check can wait for a running pass over other connections, one poll and its turn in the next pass.
    return interval + POLL_SECONDS + own + 2 * others


def read(db, connection_id, now):
    config = settings(db, connection_id)
    row = db.execute('SELECT * FROM capacity_state WHERE connection_id=?', (connection_id,)).fetchone()
    valid = bool(row and row['revision'] == config['revision'])
    report = json.loads(row['report']) if valid and row['report'] else None
    stale = bool(valid and not 0 <= now - row['checked_at'] <= stale_after(db, connection_id, config['interval_seconds']))
    summary = summarize(report, row['error'] if valid else None, stale)
    if not config['enabled']:
        summary = {**summary, 'status': 'disabled', 'message': 'Automatic capacity checks are disabled.'}
    history = [{'at': r['occurred_at'], **json.loads(r['summary'])} for r in db.execute(
        'SELECT occurred_at,summary FROM capacity_events WHERE connection_id=? AND occurred_at>=? ORDER BY id DESC LIMIT 30',
        (connection_id, now - RETENTION))]
    return {'settings': config, **summary, 'stale': stale, 'checked_at': row['checked_at'] if valid else None,
            'next_at': row['next_at'] if valid and config['enabled'] else None, 'report': report,
            'history': history, 'retention_days': 7}


def identity(db, connection_id):
    row = db.execute('SELECT url,token_id,secret,verify_tls FROM connections WHERE id=?', (connection_id,)).fetchone()
    return tuple(row) if row else None


def record(db, connection_id, config, connection_identity, report, error, now):
    if identity(db, connection_id) != connection_identity or settings(db, connection_id) != config:
        return
    summary = {**summarize(report, error), 'memory_mode': config['memory_mode']}
    encoded = json.dumps(summary, sort_keys=True)
    previous = db.execute('SELECT summary FROM capacity_events WHERE connection_id=? ORDER BY id DESC LIMIT 1', (connection_id,)).fetchone()
    if not previous or previous['summary'] != encoded:
        db.execute('INSERT INTO capacity_events(connection_id,occurred_at,summary) VALUES(?,?,?)', (connection_id, now, encoded))
    db.execute('INSERT OR REPLACE INTO capacity_state VALUES(?,?,?,?,?,?)',
               (connection_id, now, now + config['interval_seconds'], config['revision'], json.dumps(report) if report else None, error))


class Worker:
    def __init__(self, database, config_for, client_factory=ProxmoxClient):
        self.database, self.config_for, self.client_factory = database, config_for, client_factory
        self.task = None
        self.wakeup = asyncio.Event()

    def due(self, now):
        with self.database() as db:
            db.execute('DELETE FROM capacity_events WHERE occurred_at<?', (now - RETENTION,))
            db.execute('DELETE FROM capacity_state WHERE checked_at<?', (now - RETENTION,))
            result = []
            for row in db.execute('SELECT c.id,s.next_at FROM connections c LEFT JOIN capacity_state s ON s.connection_id=c.id'):
                config = settings(db, row['id'])
                if config['enabled'] and (row['next_at'] is None or row['next_at'] <= now):
                    result.append((row['id'], config, identity(db, row['id'])))
            return result

    async def collect(self, connection_id, config, connection_identity):
        report, error = None, None
        try:
            async with asyncio.timeout(COLLECT_SECONDS), self.client_factory(self.config_for(connection_id)) as client:
                await node_maintenance.verify_inventory_access(client)
                resources, native = await asyncio.gather(client.get('cluster/resources'), native_ha.inspect(client))
            def calculate():
                with self.database() as db:
                    return capacity.evaluate(db, connection_id, resources, native, time.time(), config['memory_mode'])
            report = await background.run(calculate)
        except Exception:
            error = 'Capacity check failed. Verify the connection, inventory permissions and native HA API access.'
            logging.getLogger(__name__).warning('N+1 check failed for connection %s; it will retry.', connection_id)
        def persist():
            with self.database() as db:
                record(db, connection_id, config, connection_identity, report, error, time.time())
        await background.run(persist)

    async def cycle(self):
        for args in await background.run(self.due, time.time()):
            await self.collect(*args)

    async def run(self):
        while True:
            self.wakeup.clear()
            try:
                await self.cycle()
            except Exception:
                logging.getLogger(__name__).exception('Capacity monitoring failed; retrying.')
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass

    def start(self):
        self.task = asyncio.create_task(self.run(), name='capacity-monitor')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
