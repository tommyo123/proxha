import asyncio
import json
import logging
import re
import time
from contextlib import suppress

from . import backup_guard, free_balloon, migration_poll, native_ha, telemetry
from .proxmox import ProxmoxClient, ProxmoxError, segment

SCHEMA = '''
CREATE TABLE IF NOT EXISTS native_tasks (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    upid TEXT NOT NULL, node TEXT NOT NULL, resource TEXT, kind TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
    message TEXT, source TEXT, target TEXT, origin TEXT NOT NULL DEFAULT 'Proxmox',
    reason TEXT, observed_at REAL NOT NULL, target_checked_at REAL, PRIMARY KEY(connection_id,upid)
);
CREATE INDEX IF NOT EXISTS native_tasks_expiry ON native_tasks(started_at);
CREATE TABLE IF NOT EXISTS activity_status (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    checked_at REAL, last_error TEXT, native_policy TEXT, archive_complete INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_scans (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, since REAL NOT NULL, next_start INTEGER NOT NULL DEFAULT 0,
    checked_at REAL, window_until REAL, PRIMARY KEY(connection_id,node)
);
CREATE TABLE IF NOT EXISTS guest_locations (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, node TEXT NOT NULL, observed_at REAL NOT NULL,
    PRIMARY KEY(connection_id,resource)
);
CREATE TABLE IF NOT EXISTS placement_events (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, observed_at REAL NOT NULL, previous_at REAL NOT NULL,
    source TEXT NOT NULL, target TEXT NOT NULL,
    PRIMARY KEY(connection_id,resource,observed_at)
);
CREATE INDEX IF NOT EXISTS placement_events_expiry ON placement_events(observed_at);
''' + backup_guard.SCHEMA + free_balloon.SCHEMA + native_ha.SCHEMA


def prune(db, now):
    cutoff = now - telemetry.RETENTION
    db.execute('DELETE FROM placement_events WHERE observed_at<?', (cutoff,))
    db.execute("DELETE FROM native_tasks WHERE started_at<? AND status NOT IN ('running','unknown')", (cutoff,))
    db.execute('DELETE FROM guest_locations WHERE observed_at<?', (cutoff,))
    db.execute("DELETE FROM operations WHERE julianday(created_at)<julianday(?,'unixepoch') AND NOT EXISTS (SELECT 1 FROM operation_tasks WHERE operation_id=operations.id AND status IN ('submitting','running','unknown'))", (cutoff,))


def observe_placements(db, connection_id, now):
    for sample in telemetry.latest(db, connection_id, now):
        if not sample['resource'].startswith(('qemu/', 'lxc/')) or sample['quality'] == 'unavailable' or not sample['node'] or now - sample['observed_at'] > 45:
            continue
        old = db.execute('SELECT * FROM guest_locations WHERE connection_id=? AND resource=?', (connection_id, sample['resource'])).fetchone()
        if old and old['observed_at'] >= sample['observed_at']:
            continue
        if old and old['node'] != sample['node']:
            db.execute('INSERT OR IGNORE INTO placement_events VALUES(?,?,?,?,?,?)',
                       (connection_id, sample['resource'], sample['observed_at'], old['observed_at'], old['node'], sample['node']))
        db.execute('INSERT OR REPLACE INTO guest_locations VALUES(?,?,?,?)',
                   (connection_id, sample['resource'], sample['node'], sample['observed_at']))


def record_task(db, connection_id, task, now):
    upid, node = task.get('upid'), task.get('node')
    if not isinstance(upid, str) or not upid.startswith('UPID:') or not node:
        return
    started = telemetry.numeric(task.get('starttime'))
    if started is None:
        return
    kind, identifier = str(task.get('type', 'task')), str(task.get('id', ''))
    resource = f'{"lxc" if kind.startswith("vz") else "qemu"}/{identifier}' if identifier.isdigit() and kind.startswith(('qm', 'vz')) else None
    exit_status = task.get('exitstatus') or task.get('status')
    status = 'running' if not task.get('endtime') and exit_status in (None, 'running') else 'completed' if exit_status == 'OK' else 'failed' if exit_status not in (None, 'running', 'stopped') else 'unknown'
    if started < now - telemetry.RETENTION and status not in ('running', 'unknown'):
        db.execute('DELETE FROM native_tasks WHERE connection_id=? AND upid=?', (connection_id, upid))
        return
    local = db.execute('SELECT o.payload FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.upid=?', (connection_id, upid)).fetchone()
    payload = json.loads(local['payload']) if local else {}
    source = node if kind in ('qmigrate', 'vzmigrate') else None
    reason = f'Manual {payload.get("action", "operation")} in ProxHA.' if local else 'Initiated outside ProxHA; trigger not supplied by the task API.'
    if payload.get('migration_run_id'):
        mode = {'continuous': 'Continuous balancing', 'scheduled': 'Scheduled balancing', 'placement_rules': 'Placement rule repair', 'balloon_recovery': 'Balloon recovery'}.get(payload.get('trigger'), 'Run now')
        reason = f'{mode} migration queue {payload["migration_run_id"]}; one guest at a time.'
    db.execute('''INSERT INTO native_tasks(connection_id,upid,node,resource,kind,started_at,finished_at,status,message,source,target,origin,reason,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(connection_id,upid) DO UPDATE SET finished_at=COALESCE(excluded.finished_at,native_tasks.finished_at),
        status=CASE WHEN native_tasks.status IN ('completed','failed') AND excluded.status IN ('running','unknown') THEN native_tasks.status ELSE excluded.status END,
        message=CASE WHEN native_tasks.status IN ('completed','failed') AND excluded.status IN ('running','unknown') THEN native_tasks.message ELSE excluded.message END,
        target=COALESCE(excluded.target,native_tasks.target), origin=excluded.origin,
        reason=excluded.reason, observed_at=excluded.observed_at''',
        (connection_id, upid, node, resource, kind, started, task.get('endtime'), status,
         str(exit_status or 'Running'), source, payload.get('target'), 'ProxHA' if local else 'Proxmox', reason, now))


def document(db, connection_id, resource=None):
    clauses, params = ['connection_id=?', "(started_at>=? OR status IN ('running','unknown'))"], [connection_id, time.time() - telemetry.RETENTION]
    if resource:
        clauses.append('(resource=? OR node=? OR target=?)')
        node = resource.removeprefix('node/') if resource.startswith('node/') else ''
        params.extend([resource, node, node])
    rows = [dict(row) for row in db.execute('SELECT * FROM native_tasks WHERE ' + ' AND '.join(clauses) + ' ORDER BY started_at DESC LIMIT 200', params)]
    events = [dict(row) for row in db.execute('SELECT * FROM placement_events WHERE connection_id=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 200', (connection_id, time.time() - telemetry.RETENTION))]
    if resource:
        events = [e for e in events if e['resource'] == resource or resource in (f'node/{e["source"]}', f'node/{e["target"]}')]
    status = db.execute('SELECT * FROM activity_status WHERE connection_id=?', (connection_id,)).fetchone()
    status = dict(status) if status else None
    if status:
        status['native_policy'] = json.loads(status['native_policy'] or '{}')
        status['native_ha'] = native_ha.summary(status['native_policy'])
    return {'tasks': rows, 'placements': events, 'collector': status, 'retention_days': 7,
            'coverage_note': 'Tasks visible to the saved API token. Placement changes are observations, not proof of a live migration.'}


async def reconcile(database, connection_id, client):
    await free_balloon.reconcile(database, connection_id, client)
    with database() as db:
        tasks = [dict(r) for r in db.execute("SELECT t.* FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status IN ('running','unknown') AND t.upid IS NOT NULL", (connection_id,))]
        native = [dict(r) for r in db.execute("SELECT * FROM native_tasks WHERE connection_id=? AND status IN ('running','unknown') ORDER BY started_at DESC LIMIT 100", (connection_id,))]
        migrations = {r['local_id']: dict(r) for r in db.execute('''SELECT s.*,t.id AS local_id,t.node,t.upid,
            r.finished_at AS run_finished_at,r.maintenance_node,o.payload FROM migration_run_steps s
            JOIN migration_runs r ON r.id=s.run_id JOIN operation_tasks t ON t.operation_id=s.operation_id
            JOIN operations o ON o.id=s.operation_id
            WHERE r.connection_id=? AND t.status IN ('running','unknown') AND t.upid IS NOT NULL''', (connection_id,))}
    known = [(task['upid'], {'node': task['node'], 'local_id': task['id']}) for task in tasks]
    local_upids = {task['upid'] for task in tasks}
    for task in native:
        if task['upid'] not in local_upids:
            known.append((task['upid'], {'node': task['node'], 'local_id': None}))
    semaphore = asyncio.Semaphore(6)
    async def update(upid, task):
        async with semaphore:
            try:
                step = migrations.get(task['local_id'])
                if step:
                    if step['run_finished_at'] is None:
                        return
                    expected = json.loads(step['payload']).get('expected_status', 'running')
                    try:
                        result = await migration_poll.read(client, step, expected, maintenance=bool(step['maintenance_node']), ha_managed=json.loads(step['payload']).get('ha_managed', False))
                    except ProxmoxError as exc:
                        result = exc
                    with database() as db:
                        current = db.execute('SELECT status FROM operation_tasks WHERE id=?', (task['local_id'],)).fetchone()
                        if not current or current['status'] not in ('running', 'unknown'):
                            return
                        now = time.time()
                        state, message = migration_poll.record(db, step, result, now)
                        if state == 'completed':
                            db.execute('INSERT OR IGNORE INTO placement_events VALUES(?,?,?,?,?,?)',
                                       (connection_id, step['resource'], now, step['created_at'], step['source'], step['target']))
                        if state in ('completed', 'failed'):
                            db.execute('UPDATE native_tasks SET status=?,message=?,finished_at=COALESCE(finished_at,?) WHERE connection_id=? AND upid=?',
                                       (state, message, now, connection_id, upid))
                    return
                result = await client.get(f'nodes/{segment(task["node"])}/tasks/{segment(upid)}/status')
                result = {**result, 'upid': upid, 'node': task['node']}
                if result.get('exitstatus'):
                    result['exitstatus'] = client.redact(str(result['exitstatus']))
                state = 'completed' if result.get('exitstatus') == 'OK' else ('failed' if result.get('exitstatus') else 'unknown') if result.get('status') == 'stopped' else 'running'
                message = result.get('exitstatus') or ('Task stopped without an exit status; verify in Proxmox.' if state == 'unknown' else 'Task is running.')
                with database() as db:
                    record_task(db, connection_id, result, time.time())
                    if task['local_id']:
                        db.execute('UPDATE operation_tasks SET status=?,message=? WHERE id=? AND status IN (\'running\',\'unknown\')',
                                   (state, client.redact(str(message)), task['local_id']))
            except ProxmoxError:
                return
    await asyncio.gather(*(update(upid, task) for upid, task in known))


class Worker:
    def __init__(self, database, config_for, client_factory=ProxmoxClient, lock=None):
        self.database, self.config_for, self.client_factory = database, config_for, client_factory
        self.lock = lock or asyncio.Lock()
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name='task-reconciliation')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def collect(self, connection_id, now):
        config = self.config_for(connection_id)
        with self.database() as db:
            observe_placements(db, connection_id, now)
            previous = db.execute('SELECT checked_at FROM activity_status WHERE connection_id=?', (connection_id,)).fetchone()
        async with self.client_factory(config) as client:
            await reconcile(self.database, connection_id, client)
            backup = await backup_guard.inspect(client)
            with self.database() as db:
                backup_guard.save(db, connection_id, backup, now)
            if previous and 0 <= now - (previous['checked_at'] or 0) < 60:
                return
            errors, policy = [], {}
            for path, key in [('cluster/tasks', 'tasks'), ('cluster/options', 'options'), ('cluster/ha/resources', 'ha_resources'), ('cluster/ha/rules', 'ha_rules'),
                              ('cluster/ha/status/current', 'ha_status'), ('cluster/ha/status/manager_status', 'ha_manager')]:
                try:
                    value = await client.get(path)
                    if key == 'tasks':
                        with self.database() as db:
                            for task in value:
                                task = {**task, 'status': client.redact(str(task['status']))} if task.get('status') else task
                                record_task(db, connection_id, task, now)
                    else:
                        policy[key] = {field: value[field] for field in ('crs', 'ha', 'migration') if field in value} if key == 'options' else value
                except ProxmoxError as exc:
                    errors.append(f'{key}: {exc}')
            with self.database() as db:
                maintained = [r['node'] for r in db.execute('SELECT node FROM native_ha_maintenance WHERE connection_id=?', (connection_id,))]
            if maintained and all(k in policy for k in ('ha_resources', 'ha_manager', 'ha_status')):
                resources = await client.get('cluster/resources')
                with self.database() as db:
                    for node in maintained:
                        native_ha.observe(db, connection_id, node, policy, resources, now)
            with self.database() as db:
                nodes = [s['node'] for s in telemetry.latest(db, connection_id, now) if s['resource'].startswith('node/') and s['quality'] != 'unavailable']
            complete = True
            for node in nodes:
                with self.database() as db:
                    scan = db.execute('SELECT * FROM task_scans WHERE connection_id=? AND node=?', (connection_id, node)).fetchone()
                since = max(now - telemetry.RETENTION, scan['since']) if scan else now - telemetry.RETENTION
                start = scan['next_start'] if scan else 0
                until = scan['window_until'] if scan and start else now
                finished = False
                try:
                    for _ in range(2):
                        page = await client.get(f'nodes/{segment(node)}/tasks?since={int(since)}&until={int(until)}&start={start}&limit=100')
                        with self.database() as db:
                            for task in page:
                                task = {**task, 'status': client.redact(str(task['status']))} if task.get('status') else task
                                record_task(db, connection_id, {**task, 'node': node}, now)
                        start += len(page)
                        if len(page) < 100:
                            finished = True
                            break
                    with self.database() as db:
                        db.execute('INSERT OR REPLACE INTO task_scans VALUES(?,?,?,?,?,?)',
                                   (connection_id, node, max(since, until - 120) if finished else since, 0 if finished else start, now, until))
                except ProxmoxError as exc:
                    errors.append(f'Task archive {node}: {exc}')
                complete = complete and finished
            with self.database() as db:
                missing = [dict(r) for r in db.execute("SELECT upid,node FROM native_tasks WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND target IS NULL ORDER BY COALESCE(target_checked_at,0),started_at DESC LIMIT 10", (connection_id,))]
            for task in missing:
                try:
                    lines = await client.get(f'nodes/{segment(task["node"])}/tasks/{segment(task["upid"])}/log?limit=30')
                    content = '\n'.join(str(line.get('t', '')) for line in lines)
                    match = re.search(r'(?:to node|target node)\s+[\'\"]?([A-Za-z0-9][A-Za-z0-9.-]*)', content)
                    if match:
                        with self.database() as db:
                            db.execute('UPDATE native_tasks SET target=? WHERE connection_id=? AND upid=?', (match[1], connection_id, task['upid']))
                except ProxmoxError:
                    pass
                finally:
                    with self.database() as db:
                        db.execute('UPDATE native_tasks SET target_checked_at=? WHERE connection_id=? AND upid=?', (now, connection_id, task['upid']))
            with self.database() as db:
                row = db.execute('SELECT url,token_id FROM connections WHERE id=?', (connection_id,)).fetchone()
                if row and row['url'] == config.url and row['token_id'] == config.token_id:
                    db.execute('INSERT OR REPLACE INTO activity_status VALUES(?,?,?,?,?)',
                               (connection_id, now, '; '.join(errors) or None, json.dumps(policy), int(complete and bool(nodes))))

    async def run(self):
        while True:
            with self.database() as db:
                prune(db, time.time())
                ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
            for connection_id in ids:
                try:
                    async with self.lock, asyncio.timeout(20):
                        await self.collect(connection_id, time.time())
                except Exception:
                    logging.getLogger(__name__).warning('Task collection incomplete for connection %s.', connection_id)
            await asyncio.sleep(10)
