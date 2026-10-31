import asyncio
import json
import logging
import re
import time
from collections import Counter
from contextlib import suppress
from datetime import datetime, timezone
from uuid import UUID, uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import infrastructure, adaptive, background, backup_guard, balancing, balloon, guest_policy, ha_guests, migration_failures, migration_groups, migration_poll, node_maintenance, placement_repair, placement_rules, preflight_failures, storage, telemetry
from .proxmox import CandidateRejected, ProxmoxClient, ProxmoxError, segment

SETTLE_SECONDS = 60
SCHEMA = '''
CREATE TABLE IF NOT EXISTS migration_runs (
    id TEXT PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
    status TEXT NOT NULL, message TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
    next_at REAL, task_id INTEGER REFERENCES operation_tasks(id) ON DELETE SET NULL,
    maintenance_node TEXT, restart_containers INTEGER NOT NULL DEFAULT 0, failure_message TEXT,
    trigger TEXT NOT NULL DEFAULT 'manual', proposal TEXT, guest_kind TEXT NOT NULL DEFAULT 'qemu'
);
CREATE UNIQUE INDEX IF NOT EXISTS migration_runs_active ON migration_runs(connection_id) WHERE finished_at IS NULL;
CREATE TABLE IF NOT EXISTS migration_run_steps (
    run_id TEXT NOT NULL REFERENCES migration_runs(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, score REAL NOT NULL,
    operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL, created_at REAL NOT NULL,
    reserved_memory REAL NOT NULL DEFAULT 0, reserved_cpu_cores REAL NOT NULL DEFAULT 0,
    settled_at REAL, verified_at REAL,
    poll_failures INTEGER NOT NULL DEFAULT 0, poll_error_since REAL, poll_retry_at REAL, verify_started_at REAL,
    PRIMARY KEY(run_id,resource)
);
CREATE INDEX IF NOT EXISTS migration_steps_resource ON migration_run_steps(resource,created_at);
CREATE TABLE IF NOT EXISTS migration_run_exclusions (
    run_id TEXT NOT NULL REFERENCES migration_runs(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(run_id,resource)
);
'''


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    goal: Literal['balance', 'balloon_recovery'] = 'balance'
    aggressive: bool = Field(default=False, strict=True)
    guest_kind: Literal['qemu', 'lxc'] = 'qemu'

    @model_validator(mode='after')
    def valid_goal(self):
        if self.aggressive and self.goal != 'balance':
            raise ValueError('Aggressive mode applies to balancing only.')
        if self.goal == 'balloon_recovery' and self.guest_kind != 'qemu':
            raise ValueError('Balloon recovery applies to VMs only.')
        return self


class MigrationDeferred(CandidateRejected):
    def __init__(self, message, capacity=None):
        super().__init__(message)
        self.capacity = capacity


class QueueBusy(ProxmoxError):
    pass


def bwlimit(config):
    # Proxmox expects KiB/s; zero keeps the datacenter migration bandwidth default.
    limit = config.get('migration_bandwidth_limit_mib') or 0
    return {'bwlimit': limit * 1024} if limit else {}


def target_limit(config, limit):
    # A per-host limit of zero leaves only the queue's own parallel limit.
    return min(limit, config['migration_target_concurrency'] or limit)


def target_slots(steps, target, config):
    limit = config['migration_target_concurrency'] or len(steps) + 1
    return limit - sum(s['settled_at'] is None and s['target'] == target for s in steps)


def upgrade(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(migration_runs)')}
    if 'maintenance_node' not in columns:
        db.execute('ALTER TABLE migration_runs ADD COLUMN maintenance_node TEXT')
        db.execute('ALTER TABLE migration_runs ADD COLUMN restart_containers INTEGER NOT NULL DEFAULT 0')
    if 'failure_message' not in columns:
        db.execute('ALTER TABLE migration_runs ADD COLUMN failure_message TEXT')
    if 'trigger' not in columns:
        db.execute("ALTER TABLE migration_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
    if 'proposal' not in columns:
        db.execute('ALTER TABLE migration_runs ADD COLUMN proposal TEXT')
    if 'guest_kind' not in columns:
        db.execute("ALTER TABLE migration_runs ADD COLUMN guest_kind TEXT NOT NULL DEFAULT 'qemu'")
        db.execute("UPDATE migration_runs SET guest_kind='all' WHERE maintenance_node IS NOT NULL")
        db.execute('UPDATE migration_runs SET restart_containers=0 WHERE maintenance_node IS NULL AND finished_at IS NULL')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(migration_run_steps)')}
    for name in ('reserved_memory', 'reserved_cpu_cores', 'settled_at', 'verified_at'):
        if name not in columns:
            db.execute(f'ALTER TABLE migration_run_steps ADD COLUMN {name} REAL' + (' NOT NULL DEFAULT 0' if name.startswith('reserved_') else ''))
    for name in ('poll_failures', 'poll_error_since', 'poll_retry_at', 'verify_started_at'):
        if name not in columns:
            db.execute(f'ALTER TABLE migration_run_steps ADD COLUMN {name} ' + ('INTEGER NOT NULL DEFAULT 0' if name == 'poll_failures' else 'REAL'))
    db.execute('''UPDATE migration_run_steps AS s SET verified_at=(
        SELECT MAX(p.observed_at) FROM placement_events p JOIN migration_runs r ON r.connection_id=p.connection_id
        WHERE r.id=s.run_id AND p.resource=s.resource AND p.source=s.source AND p.target=s.target
        AND p.previous_at=s.created_at AND p.observed_at>=s.created_at
    ) WHERE s.verified_at IS NULL AND EXISTS (
        SELECT 1 FROM operation_tasks t WHERE t.operation_id=s.operation_id AND t.status='completed')''')


def active(db, connection_id):
    return db.execute('SELECT * FROM migration_runs WHERE connection_id=? AND finished_at IS NULL', (connection_id,)).fetchone()


def document(db, connection_id, run_id=None):
    row = db.execute('SELECT * FROM migration_runs WHERE connection_id=?' + (' AND id=?' if run_id else ' ORDER BY (finished_at IS NULL) DESC,created_at DESC,rowid DESC LIMIT 1'),
                     (connection_id, run_id) if run_id else (connection_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['active'] = row['finished_at'] is None
    result['proposal'] = json.loads(row['proposal']) if row['proposal'] else None
    result['steps'] = [dict(step) for step in db.execute('''SELECT s.*,t.status,t.message,t.upid FROM migration_run_steps s
        LEFT JOIN operation_tasks t ON t.operation_id=s.operation_id WHERE run_id=? ORDER BY s.created_at''', (row['id'],))]
    result['completed'] = sum(step['status'] == 'completed' for step in result['steps'])
    result['failed'] = sum(step['status'] == 'failed' for step in result['steps'])
    result['exclusions'] = [dict(r) for r in db.execute('SELECT resource,reason FROM migration_run_exclusions WHERE run_id=? ORDER BY created_at', (row['id'],))]
    from . import placement_queue
    batch = placement_queue.current(db, row['id'])
    result['placement_batch'] = {k: batch[k] for k in ('id', 'status', 'message')} if batch else None
    result['concurrency'] = balancing.settings(db, connection_id)['maintenance_concurrency'] if row['maintenance_node'] else balancing.settings(db, connection_id)['placement_concurrency'] if batch else 1
    result['in_flight'] = sum(step['status'] in ('submitting', 'running', 'unknown') for step in result['steps'])
    return result


def update(db, run_id, status, message, now, *, finish=False, next_at=None):
    db.execute('UPDATE migration_runs SET status=?,message=?,updated_at=?,finished_at=?,next_at=? WHERE id=? AND finished_at IS NULL',
               (status, message, now, now if finish else None, next_at, run_id))


def start(db, connection_id, request_id, now, maintenance_node=None, restart_containers=False, trigger='manual', proposal=None, guest_kind='qemu'):
    guest_kind = 'all' if maintenance_node else guest_kind
    if guest_kind not in ('qemu', 'lxc', 'all') or guest_kind == 'all' and not maintenance_node:
        raise ProxmoxError('Choose a VM or LXC balancing job.')
    if trigger in ('continuous', 'balloon_recovery', 'placement_rules') and guest_kind != 'qemu':
        raise ProxmoxError('This automatic job only migrates VMs. Use the VM + LXC schedule or Run VM + LXC now.')
    restart_containers = restart_containers if maintenance_node else guest_kind == 'lxc'
    existing = db.execute('SELECT connection_id,maintenance_node,restart_containers,trigger,guest_kind FROM migration_runs WHERE id=?', (request_id,)).fetchone()
    if existing:
        if existing['connection_id'] != connection_id or existing['maintenance_node'] != maintenance_node or bool(existing['restart_containers']) != restart_containers or existing['trigger'] != trigger or existing['guest_kind'] != guest_kind:
            raise ProxmoxError('Request ID belongs to a different queue request.')
        return document(db, connection_id, request_id)
    current = active(db, connection_id)
    if current:
        if current['maintenance_node'] != maintenance_node or current['trigger'] != trigger or current['guest_kind'] != guest_kind:
            raise ProxmoxError('Stop the current queue before starting a different evacuation or balancing run.')
        return document(db, connection_id, current['id'])
    if balancing.settings(db, connection_id)['paused']:
        raise ProxmoxError('Planning is paused. Unpause planning before starting a migration queue.')
    ensure_idle(db, connection_id)
    db.execute('INSERT INTO migration_runs(id,connection_id,created_at,updated_at,status,message,maintenance_node,restart_containers,trigger,proposal,guest_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (request_id, connection_id, now, now, 'queued', 'Queued. Checking the next guest before migration.', maintenance_node, int(restart_containers), trigger, json.dumps(proposal) if proposal else None, guest_kind))
    return document(db, connection_id, request_id)


def ensure_scope(run, resource):
    if resource.partition('/')[0] not in migration_groups.balancing_types(run['guest_kind']):
        raise CandidateRejected('Guest type is outside this balancing job. The VM-only job cannot migrate containers.')


def enabled(run, config):
    if config['paused'] or run['trigger'] == 'continuous' and not config['continuous_enabled']:
        return False
    if run['trigger'] == 'scheduled':
        return config['container_schedule']['enabled'] if run['guest_kind'] == 'lxc' else config['distribution_enabled']
    return True


def stop(db, connection_id, run_id, now):
    run = document(db, connection_id, run_id)
    if not run or not run['active']:
        return run
    if run['trigger'] == 'placement_rules':
        config = balancing.settings(db, connection_id)
        revision = config.pop('revision')
        config['placement_repair_enabled'] = False
        db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)', (connection_id, json.dumps(config), revision + 1))
    db.execute('UPDATE migration_runs SET stop_requested=1 WHERE id=?', (run_id,))
    running = any(step['settled_at'] is None for step in run['steps'])
    if not running:
        db.execute("UPDATE placement_batches SET finished_at=?,status='stopped' WHERE run_id=? AND finished_at IS NULL", (now, run_id))
    update(db, run_id, 'stopping' if running else 'stopped',
           'Stop requested. Submitted migrations will finish; no new migrations will start.' if running else 'Queue stopped. No further migrations will start.',
           now, finish=not running)
    if run['maintenance_node'] and not running:
        node_maintenance.set_state(db, connection_id, run['maintenance_node'], 'blocked', 'Evacuation stopped. Host remains excluded; resume evacuation or exit maintenance.', now)
    return document(db, connection_id, run_id)


def cancel_backup(db, run, message, now):
    message += (' Aggressive run cancelled; start a new run after backup finishes.' if run['trigger'] == 'aggressive_manual' else ' Scheduled run cancelled; the next schedule will reassess.') + ' Submitted migrations finish normally.'
    db.execute('UPDATE migration_runs SET stop_requested=1,failure_message=? WHERE id=?', (message, run['id']))
    pending = db.execute('SELECT 1 FROM migration_run_steps WHERE run_id=? AND settled_at IS NULL', (run['id'],)).fetchone()
    if not pending:
        db.execute("UPDATE placement_batches SET finished_at=?,status='cancelled_backup',message=? WHERE run_id=? AND finished_at IS NULL", (now, message, run['id']))
    update(db, run['id'], 'cancelling_backup' if pending else 'cancelled_backup', message, now, finish=not pending)


def recover(db, now):
    db.execute("UPDATE placement_batches SET status='interrupted',finished_at=?,message='Server restarted; verify submitted tasks before resuming.' WHERE finished_at IS NULL", (now,))
    db.execute("UPDATE node_maintenance SET state='blocked',message='Server restarted. Host remains excluded. Resume evacuation when ready.',updated_at=? WHERE enabled=1 AND state IN ('evacuating','waiting')", (now,))
    db.execute("UPDATE migration_runs SET status='interrupted',stop_requested=1,finished_at=?,updated_at=?,message=? WHERE finished_at IS NULL",
               (now, now, 'Server restarted. Queue stopped; any submitted Proxmox task continues independently. Check task status before starting again.'))
    db.execute("UPDATE node_power_actions SET status='unknown',message='Server restarted during submission. Check the host before retrying.',updated_at=? WHERE status='submitting'", (now,))


def prune(db, now):
    db.execute("""DELETE FROM migration_runs WHERE finished_at<? AND NOT EXISTS (
        SELECT 1 FROM migration_run_steps s JOIN operation_tasks t ON t.operation_id=s.operation_id
        WHERE s.run_id=migration_runs.id AND t.status IN ('submitting','running','unknown'))""", (now - telemetry.RETENTION,))
    node_maintenance.prune(db, now)


def ensure_idle(db, connection_id, run_id=None):
    if db.execute("""SELECT 1 FROM operation_tasks t JOIN operations o ON o.id=t.operation_id
        LEFT JOIN migration_run_steps s ON s.operation_id=o.id WHERE o.connection_id=?
        AND t.status IN ('submitting','running','unknown') AND (? IS NULL OR s.run_id IS NULL OR s.run_id!=?)""", (connection_id, run_id, run_id)).fetchone():
        raise QueueBusy('An active or unresolved operation blocks the migration queue. Check Tasks & Events.')
    tasks = db.execute("""SELECT n.* FROM native_tasks n WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND status IN ('running','unknown')
        AND NOT EXISTS (SELECT 1 FROM migration_run_steps s JOIN operation_tasks t ON t.operation_id=s.operation_id WHERE s.run_id=? AND t.upid=n.upid)""", (connection_id, run_id)).fetchall()
    # Native HA performs requested HA moves as separate LRM tasks without the hand-off task ID.
    if any(not ha_guests.handoff_task(db, connection_id, run_id, task) for task in tasks):
        raise QueueBusy('An active or unresolved Proxmox migration blocks the queue.')


def check_devices(guest, config, check, inventories, network, target, ha_unit=(), restart_containers=False):
    source, kind, running = guest['node'], guest['type'], guest['status'] == 'running'
    if not isinstance(config.get('digest'), str):
        raise CandidateRejected('Guest configuration could not be verified.')
    if kind == 'qemu' and (bool(check.get('running')) != running or 'running' not in check or check.get('local_disks') != [] or check.get('local_resources') != []):
        raise CandidateRejected('Proxmox reports local disks/devices or an unverified live-migration state.')
    dependencies = {ha_guests.resource(k) for k in check.get('dependent-ha-resources') or []}
    if dependencies and (None in dependencies or not dependencies.issubset(ha_unit)):
        raise CandidateRejected('Native HA would co-migrate guests outside the checked group.')
    if kind == 'qemu' and (target not in check.get('allowed_nodes', []) or check.get('not_allowed_nodes', {}).get(target)):
        raise CandidateRejected('Proxmox migration preconditions do not permit this target independently.')
    if check.get('mapped-resources') or check.get('mapped-resource-info'):
        raise CandidateRejected('Mapped devices require manual migration review.')
    if kind == 'lxc' and running and not restart_containers:
        raise CandidateRejected('Container restart migration requires explicit downtime approval.')
    if kind == 'lxc' and (bool(check.get('running')) != running or 'running' not in check or target not in check.get('allowed-nodes', []) or check.get('not-allowed-nodes', {}).get(target)):
        raise CandidateRejected('Proxmox container migration preconditions do not permit this target independently.')
    inspected = {**storage.inspect(config, source, inventories), 'node': source}
    reason = storage.block_reason(inspected, source, target)
    if reason:
        raise CandidateRejected(reason)
    bridges = {r.get('iface') for r in network if r.get('active') in (1, True)}
    for key, value in config.items():
        if re.fullmatch(r'net\d+', key):
            bridge = re.search(r'(?:^|,)bridge=([^,]+)', str(value))
            if not bridge or bridge[1] not in bridges:
                raise CandidateRejected('A guest network bridge is missing or inactive on the target.')


async def live_preflight(client, move, maintenance=False, restart_containers=False, allowed_upids=(), ha_allowed=False, host_performance=None, ha_unit=(), infrastructure_status=None):
    source, target, resource = move['source'], move['target'], move['resource']
    infra = (infrastructure_status or {}).get(target, {})
    if infra.get('block_reason'):
        raise MigrationDeferred(infra['block_reason'])
    vmid = int(resource.split('/')[1])
    kind = resource.split('/')[0]
    expected_status = move.get('expected_status', 'running')
    running = expected_status == 'running'
    base = f'nodes/{segment(source)}/{kind}/{vmid}'
    resources, quorum, tasks, ha, ha_status = await asyncio.gather(*(client.get(path) for path in
        ('cluster/resources', 'cluster/status', 'cluster/tasks', 'cluster/ha/resources', 'cluster/ha/status/current')))
    await backup_guard.require_clear(client, resources)
    if not any(row.get('type') == 'cluster' and row.get('quorate') in (1, True) for row in quorum):
        raise ProxmoxError('Cluster quorum could not be confirmed.')
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    guest = next((r for r in resources if r.get('id') == resource), None)
    if not guest or guest.get('type') != kind or guest.get('node') != source or guest.get('status') != expected_status or (not maintenance and (kind not in ('qemu', 'lxc') or not running)):
        raise CandidateRejected('Guest placement or running state changed. Reassessment is required.')
    if any(nodes.get(node, {}).get('status') != 'online' for node in (source, target)):
        raise CandidateRejected('Both migration hosts must be online.')
    if any(t.get('type') in ('qmigrate', 'vzmigrate') and not t.get('endtime') and t.get('status') in (None, 'running', 'unknown') and t.get('upid') not in allowed_upids for t in tasks):
        raise QueueBusy('Another Proxmox migration is active or unresolved.')
    entry = next((r for r in ha if r.get('sid') in (f'vm:{vmid}', f'ct:{vmid}')), None)
    if entry:
        if ha_allowed and not ha_guests.stable(ha_status, entry['sid'], source, time.time()):
            raise CandidateRejected('Native HA manager status is stale, disarmed or the service is not stably started on its source.')
        try:
            rules = await client.get('cluster/ha/rules') if ha_allowed else None
        except ProxmoxError:
            rules = None
        if rules is None or not ha_guests.eligible(entry, rules, entry['sid']):
            raise CandidateRejected('Native HA owns this guest; queue migration is blocked.')
        native = {'ha_resources': ha, 'ha_rules': rules, 'ha_status': ha_status}
        peers = ha_guests.cohort(native, entry['sid'])
        if peers - {resource} and not peers.issubset(ha_unit):
            raise CandidateRejected('Native HA affinity requires the complete group to be preflighted together.')
        constraints = ha_guests.placement_constraints(native, resources)
        locations = {r['id']: r['node'] for r in resources if r.get('type') in ('qemu', 'lxc') and r.get('status') == 'running'}
        for rule in constraints:
            if resource not in rule['guests']:
                continue
            if rule['kind'] == 'require_hosts' and target not in rule['hosts']:
                raise CandidateRejected(f'{rule["name"]} does not permit this target or would fail back to a preferred host.')
            if rule['kind'] == 'apart' and any(locations.get(peer) == target for peer in rule['guests'] if peer != resource):
                raise CandidateRejected(f'{rule["name"]} prevents co-location on this target.')
        move['ha_cohort'] = sorted(peers)
        # Proxmox hands an HA-managed migration to the HA manager, which then moves the guest.
        move['ha_managed'] = True
    if any((r.get('node') == target or r.get('node') == source and not maintenance) and node_maintenance.native_maintenance(r) for r in ha_status):
        raise CandidateRejected('A participating host is in maintenance mode.')

    async def read_candidate(path):
        try:
            return await client.get(path)
        except ProxmoxError as exc:
            # Gateway errors after endpoint failover mean the API is unavailable; the run waits instead of excluding guests.
            if not exc.uncertain and not exc.unavailable and (exc.status_code in (400, 403, 404, 409) or (exc.status_code or 0) >= 500):
                raise CandidateRejected(str(exc)) from exc
            raise

    current, config, check, target_status, network, source_storage, target_storage = await asyncio.gather(*(
        read_candidate(path) for path in (base + '/status/current', base + '/config?current=1',
        base + '/migrate?target=' + segment(target), f'nodes/{segment(target)}/status',
        f'nodes/{segment(target)}/network', f'nodes/{segment(source)}/storage', f'nodes/{segment(target)}/storage')))
    if current.get('status') != expected_status or (kind == 'qemu' and running and current.get('qmpstatus') != 'running') or current.get('ha', {}).get('managed') and not move.get('ha_managed') or config.get('lock') or guest.get('lock') or (running and config.get('template')):
        raise CandidateRejected('Guest is locked, paused, a template, or HA-managed.')
    inventories = {node: {r['storage']: r for r in rows} for node, rows in ((source, source_storage), (target, target_storage))}
    check_devices(guest, config, check, inventories, network, target, ha_unit, restart_containers)
    if not running:
        await backup_guard.require_clear(client)
        return guest, nodes, base + '/migrate'
    memory = target_status.get('memory', {})
    total, used = memory.get('total'), memory.get('used')
    cores, cpu = target_status.get('cpuinfo', {}).get('cpus'), target_status.get('cpu')
    guest_ram, guest_cores, guest_cpu = current.get('maxmem'), current.get('cpus'), current.get('cpu')
    if any(not isinstance(v, (int, float)) or v < 0 for v in (total, used, cores, cpu, guest_ram, guest_cores, guest_cpu)) or not all((total, cores, guest_ram, guest_cores)):
        raise MigrationDeferred('Live target or guest capacity readings are incomplete; waiting for fresh measurements.')
    usage_known = isinstance(current.get('mem'), (int, float)) and current['mem'] > 0
    memory_mode = move.get('memory_mode', 'configured') if maintenance and usage_known and not config.get('hugepages') else 'configured'
    resident = current.get('memhost') if current.get('memhost') is not None else current.get('mem') if kind == 'lxc' else None
    ram = max(move['reserved_memory'], node_maintenance.memory_demand(guest_ram, current.get('mem'), resident, memory_mode))
    speed = host_performance or {}
    scale = speed.get(source, 1) / speed.get(target, 1)
    reserved_cpu = max(move['reserved_cpu_cores'], guest_cpu * guest_cores * scale)
    budget = node_maintenance.capacity_budget(total, used, cores, cpu, ram, reserved_cpu,
        move.get('live_pending_memory', move.get('pending_memory', 0)), move.get('live_pending_cpu_cores', move.get('pending_cpu_cores', 0)), memory_mode)
    reason = (placement_repair.capacity_reason(total, used, ram, move.get('pending_memory', 0), cores, cpu,
                                               reserved_cpu, move.get('pending_cpu_cores', 0))
              if move.get('rule_repair') else node_maintenance.capacity_reason(budget))
    if reason:
        raise MigrationDeferred(reason, budget)
    if move.get('balloon_admission') and not maintenance:
        target_config = balloon.node_config(await client.get(f'nodes/{segment(target)}/config'), time.time())
        threshold = target_config['target_percent']
        if threshold is None:
            raise CandidateRejected('Target ballooning threshold could not be verified.')
        pending_recovery = move['balloon_admission']['target_recoverable']
        if used + ram + move.get('pending_memory', 0) + pending_recovery > total * max(0, threshold - balloon.MARGIN_PERCENT) / 100:
            raise MigrationDeferred('Target no longer has room for guest RAM and balloon recovery below its configured threshold.')
    move.update(reserved_memory=ram, reserved_cpu_cores=reserved_cpu, memory_mode=memory_mode, configured_memory=guest_ram, resident_memory=resident)
    await backup_guard.require_clear(client)
    return guest, nodes, base + '/migrate'


class Worker:
    def __init__(self, database, config_for, lock, client_factory=ProxmoxClient):
        self.database, self.config_for, self.lock, self.client_factory = database, config_for, lock, client_factory
        self.task = None
        self.rule_checks = {}
        self.native_checks = {}

    def start(self):
        self.task = asyncio.create_task(self.run(), name='migration-queue')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def finish(self, run_id, status, message, now):
        with self.database() as db:
            update(db, run_id, status, message, now, finish=True)
            run = db.execute('SELECT * FROM migration_runs WHERE id=?', (run_id,)).fetchone()
            if run and run['maintenance_node']:
                node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'blocked', message, now)

    def continue_after_failure(self, run, step, message, now):
        with self.database() as db:
            migration_failures.record(db, step, message, now)
            db.execute('UPDATE migration_run_steps SET settled_at=? WHERE operation_id=?', (now, step['operation_id']))
            db.execute("UPDATE operation_tasks SET status='failed',message=? WHERE operation_id=?", (message, step['operation_id']))
            db.execute('UPDATE migration_runs SET task_id=NULL WHERE id=?', (run['id'],))
            current = document(db, run['connection_id'], run['id'])
            stopping = current['stop_requested'] or balancing.settings(db, run['connection_id'])['paused']
            continuous = run['trigger'] == 'continuous'
            note = f'{step["resource"]} migration failed: {message}. Failure penalty: 200 points, decaying by one point per minute.'
            if run['trigger'] in ('scheduled', 'aggressive_manual'):
                note += ' This run ignores this penalty.'
            cancelled = run['trigger'] in ('scheduled', 'aggressive_manual') and current['failure_message']
            update(db, run['id'], 'cancelled_backup' if cancelled else 'stopped' if stopping else 'completed_with_errors' if continuous else 'waiting',
                   current['failure_message'] if cancelled else note + (' Queue stopped by request.' if stopping else ' Reassessing other guests; this guest is skipped for this run.'),
                   now, finish=stopping or continuous, next_at=None if stopping or continuous else now + 15)

    async def poll(self, client, run, task, now):
        with self.database() as db:
            step = dict(db.execute('SELECT * FROM migration_run_steps WHERE operation_id=?', (task['operation_id'],)).fetchone())
            payload = json.loads(db.execute('SELECT payload FROM operations WHERE id=?', (task['operation_id'],)).fetchone()['payload'])
        if step['poll_retry_at'] and now < step['poll_retry_at']:
            return
        try:
            result = await migration_poll.read(client, {**step, **task}, payload.get('expected_status', 'running'), ha_managed=payload.get('ha_managed', False))
        except ProxmoxError as exc:
            result = exc
        with self.database() as db:
            state, message = migration_poll.record(db, step, result, now)
            if state in ('status_retry', 'verifying'):
                update(db, run['id'], state, message, now)
                return
            if state == 'running':
                if run['status'] in ('status_retry', 'verifying'):
                    update(db, run['id'], 'stopping' if run['stop_requested'] else 'migrating', message, now)
                return
        if state == 'failed':
            self.continue_after_failure(run, step, message, now)
            return
        if state != 'completed':
            self.finish(run['id'], 'failed', message + ' No further migrations submitted.', now)
            return
        with self.database() as db:
            db.execute('INSERT OR IGNORE INTO placement_events VALUES(?,?,?,?,?,?)',
                       (run['connection_id'], step['resource'], now, step['created_at'], step['source'], step['target']))
            db.execute('UPDATE migration_run_steps SET verified_at=?,settled_at=? WHERE run_id=? AND resource=?', (now, now, run['id'], step['resource']))
            db.execute('UPDATE migration_runs SET task_id=NULL WHERE id=?', (run['id'],))
            current = document(db, run['connection_id'], run['id'])
            stopping = current['stop_requested'] or balancing.settings(db, run['connection_id'])['paused']
            if run['trigger'] in ('scheduled', 'aggressive_manual') and current['failure_message']:
                update(db, run['id'], 'cancelled_backup', current['failure_message'], now, finish=True)
                return
            if run['trigger'] == 'continuous' and not stopping:
                update(db, run['id'], 'completed', 'Continuous balancing move completed. The controller will settle and reassess before another move.', now, finish=True)
                return
            update(db, run['id'], 'stopped' if stopping else 'settling',
                   'Queue stopped after the current migration completed.' if stopping else 'Migration completed. Waiting 60 seconds for fresh measurements before selecting the next guest.',
                   now, finish=stopping, next_at=None if stopping else now + SETTLE_SECONDS)

    def plan(self, run, now, automatic):
        run_id = run['id']
        with self.database() as db:
            ensure_idle(db, run['connection_id'])
            moved = {r['resource'] for r in db.execute('SELECT resource FROM migration_run_steps WHERE run_id=?', (run_id,))}
            moved.update(r['resource'] for r in db.execute('SELECT resource FROM migration_run_exclusions WHERE run_id=?', (run_id,)))
            if run['trigger'] == 'placement_rules':
                result = balancing.evaluate(db, run['connection_id'], now, run_now=True, rule_only=True, excluded_resources=moved, guest_kind=run['guest_kind'])
                moves = [m for m in result['plan'] if m.get('rule_repair')]
            elif automatic:
                from . import continuous
                controller = continuous.read(db, run['connection_id'])
                proposal = json.loads(run['proposal'])
                result = balancing.evaluate(db, run['connection_id'], now, controller.get('observation'), manual=False, continuous=True)
                moves = [m for m in continuous.eligible(result) if all(m[k] == proposal[k] for k in ('resource', 'source', 'target'))]
                if result['policy_key'] != proposal['policy_key'] or not moves:
                    update(db, run_id, 'cancelled', 'Automatic recommendation changed or no longer passes history, observation and policy checks. Reassessment is required.', now, finish=True)
                    return
            else:
                result = balancing.save_evaluation(db, run['connection_id'], now, run_now=True, excluded_resources=moved, recovery_only=run['trigger'] == 'balloon_recovery', scheduled=run['trigger'] in ('scheduled', 'aggressive_manual'), aggressive=run['trigger'] == 'aggressive_manual', guest_kind=run['guest_kind'])
                moves = [m for m in result['plan'] if m['resource'] not in moved and m.get('policy_allowed') and not m.get('requires_storage_review') and guest_policy.read(db, run['connection_id'], m['resource'])['stickiness'] < 10]
            if not moves:
                if any(h.get('cpu_settling') for h in result['hosts']):
                    update(db, run_id, 'waiting', 'Waiting for fresh host CPU measurements after migration.', now, next_at=now + 15)
                    return
                if run['trigger'] == 'balloon_recovery':
                    recovery = result['balloon_recovery']
                    waiting = recovery['eligible_bytes'] > max(balloon.MIN_GAIN, sum(h['guests'] for h in recovery['hosts']) * 100 * balloon.MIB)
                    if waiting and now - run['created_at'] < 900:
                        update(db, run_id, 'recovering', 'Capacity is available. Watching native Proxmox balloon recovery; no beneficial migration is needed. Actual RAM return is shown in Overview. Stop queue ends monitoring.', now, next_at=now + 15)
                        return
                    blocked = recovery['blocked_bytes'] > balloon.MIN_GAIN or any(h['state'] in ('unknown', 'unverified') or h['unmanaged_bytes'] > balloon.MIN_GAIN for h in recovery['hosts'])
                    blocked = blocked or waiting
                    update(db, run_id, 'blocked' if blocked else 'completed',
                           'Recovery is incomplete. Review candidate exclusions; if capacity is available but RAM does not return, check pvestatd, guest balloon drivers and shares. No guest memory settings were changed.' if blocked else
                           'Recovery placement finished. Proxmox returns RAM gradually where capacity is available; monitor Balloon recovery for measured progress.', now, finish=True)
                    return
                unverified = [g for g in result.get('distribution', {}).get('groups', []) if not g['available']]
                if unverified:
                    detail = '; '.join(f'{g["name"]}: {g["reason"]}' for g in unverified)
                    update(db, run_id, 'blocked', 'Workload distribution is unverified. ' + detail, now, finish=True)
                    return
                imbalanced = [h['node'] for h in result['hosts'] if h['state'] in ('imbalanced', 'critical', 'insufficient_data')]
                if imbalanced:
                    reasons = Counter(reason for candidate in result['candidates'] if candidate['source'] in imbalanced for reason in candidate.get('reasons', []))
                    detail = '; '.join(f'{reason} ({count})' for reason, count in reasons.most_common(3)) or result['message']
                    update(db, run_id, 'blocked', f'Balancing remains incomplete on {", ".join(imbalanced)}. {detail}', now, finish=True)
                else:
                    failed = document(db, run['connection_id'], run_id)['failed']
                    update(db, run_id, 'completed_with_errors' if failed else 'completed',
                           'No further eligible moves. Failed guests were skipped; review their task errors.' if failed else
                           'No further beneficial moves within the selected balancing thresholds. All eligible hosts are within the distribution targets.', now, finish=True)
                return
            move = moves[0]
            update(db, run_id, 'checking', f'Checking {move["name"]} ({move["resource"]}): {move["source"]} → {move["target"]}.', now)
            return move

    async def step(self, run_id, now=None):
        now = time.time() if now is None else now
        async with self.lock:
            with self.database() as db:
                row = db.execute('SELECT * FROM migration_runs WHERE id=? AND finished_at IS NULL', (run_id,)).fetchone()
                if not row:
                    return
                run = dict(row)
                automatic = run['trigger'] == 'continuous'
                scheduled_run = run['trigger'] in ('scheduled', 'aggressive_manual')
            from . import placement_queue
            with self.database() as db:
                batch = placement_queue.current(db, run_id)
            if batch:
                await placement_queue.step(self, run, now)
                return
            if run['maintenance_node']:
                from . import evacuation_queue
                await evacuation_queue.step(self, run, now)
                return
            with self.database() as db:
                task = db.execute('SELECT * FROM operation_tasks WHERE id=?', (run['task_id'],)).fetchone() if run['task_id'] else None
                config = balancing.settings(db, run['connection_id'])
                if not task and (run['stop_requested'] or not enabled(run, config)):
                    update(db, run_id, 'stopped', 'Queue stopped before the next migration.', now, finish=True)
                    return
                if not task and not scheduled_run and run['next_at'] and now < run['next_at']:
                    return
            move = None
            try:
                async with self.client_factory(self.config_for(run['connection_id'])) as client:
                    if scheduled_run and not run['stop_requested']:
                        with self.database() as db:
                            backup = backup_guard.read(db, run['connection_id'], now)
                        if backup.get('checked_at') is None or now - backup['checked_at'] >= 15 or backup['state'] == 'unknown':
                            backup = await backup_guard.inspect(client)
                            with self.database() as db:
                                backup_guard.save(db, run['connection_id'], backup, now)
                        if backup['blocked']:
                            with self.database() as db:
                                cancel_backup(db, run, backup['message'], now)
                                run = document(db, run['connection_id'], run_id)
                            if not task:
                                return
                    if task:
                        await self.poll(client, run, dict(task), now)
                        return
                    if run['next_at'] and now < run['next_at']:
                        return
                    backup = await backup_guard.inspect(client)
                    with self.database() as db:
                        backup_guard.save(db, run['connection_id'], backup, now)
                    if backup['blocked']:
                        raise backup_guard.BackupBlocked(backup['message'])
                    move = await background.run(self.plan, run, now, automatic)
                    if move is None:
                        return
                    for member in move.get('unit_moves', [move]):
                        ensure_scope(run, member['resource'])
                    if move.get('unit_moves'):
                        await placement_queue.step(self, run, now, move)
                        return
                    with self.database() as db:
                        host_performance = balancing.performance(db, run['connection_id'])
                        infrastructure_status = infrastructure.latest(db, run['connection_id'], now)
                    guest, nodes, path = await live_preflight(client, move, restart_containers=bool(run['restart_containers']),
                                                              ha_allowed=config['ha_guests_enabled'], host_performance=host_performance, infrastructure_status=infrastructure_status)
                    with self.database() as db:
                        db.execute('BEGIN IMMEDIATE')
                        current = document(db, run['connection_id'], run_id)
                        if not current['active'] or current['stop_requested']:
                            return
                        current_config = balancing.settings(db, run['connection_id'])
                        if automatic and move['urgency'] != 'critical' and not adaptive.hourly_budget(db, run['connection_id'], time.time(), current_config['continuous_migrations_per_hour'])['remaining']:
                            raise QueueBusy('Continuous hourly migration budget is exhausted; waiting for the next allowance.')
                        if not enabled(run, current_config):
                            update(db, run_id, 'stopped', 'Planning or this schedule was disabled before submission.', now, finish=True)
                            return
                        ensure_idle(db, run['connection_id'])
                        _, overrides = await placement_rules.live_check(client, db, run['connection_id'], {move['resource']: move['target']})
                        move['placement_overrides'] = overrides
                        excluded_nodes = node_maintenance.excluded(db, run['connection_id'])
                        if move['target'] in excluded_nodes or move['source'] in excluded_nodes:
                            raise CandidateRejected('Host entered maintenance before submission.')
                        groups = migration_groups.list_groups(db, run['connection_id'])
                        policy = migration_groups.automatic_migration_policy(groups, nodes, guest, move['target'],
                            restart_containers=bool(run['restart_containers']),
                            stickiness=guest_policy.read(db, run['connection_id'], move['resource'])['stickiness'])
                        if not policy['allowed']:
                            raise CandidateRejected(policy['reason'])
                        if time.time() - now > 45:
                            raise ProxmoxError('Preflight took too long; capacity evidence expired. Run again.')
                        infrastructure_reason = infrastructure.latest(db, run['connection_id'], time.time()).get(move['target'], {}).get('block_reason')
                        if infrastructure_reason:
                            raise MigrationDeferred(infrastructure_reason)
                        operation_id = str(uuid4())
                        payload = {'request_id': operation_id, 'action': 'migrate', 'vmids': [guest['vmid']], 'target': move['target'], 'with_local_disks': False, 'restart_containers': bool(run['restart_containers']), 'migration_run_id': run_id, 'maintenance_node': run['maintenance_node'], 'expected_status': guest['status'], 'trigger': run['trigger'], 'score': move['score'], 'score_breakdown': move['score_breakdown'], 'placement_overrides': move.get('placement_overrides', []), 'ha_managed': bool(move.get('ha_managed'))}
                        db.execute('INSERT INTO operations VALUES(?,?,?,?)', (operation_id, run['connection_id'], json.dumps(payload), datetime.fromtimestamp(now, timezone.utc).isoformat()))
                        task_id = db.execute('INSERT INTO operation_tasks(operation_id,vmid,name,node,kind,status,message) VALUES(?,?,?,?,?,?,?)',
                            (operation_id, guest['vmid'], guest.get('name'), move['source'], guest['type'], 'submitting', 'Submitting one migration.')).lastrowid
                        db.execute('INSERT INTO migration_run_steps(run_id,resource,source,target,score,operation_id,created_at) VALUES(?,?,?,?,?,?,?)', (run_id, move['resource'], move['source'], move['target'], move['score'], operation_id, now))
                        db.execute('UPDATE migration_runs SET task_id=? WHERE id=?', (task_id, run_id))
                        method = 'Restart-migrating container' if guest['type'] == 'lxc' else 'Live-migrating VM'
                        update(db, run_id, 'migrating', f'{method} {guest.get("name") or move["resource"]}: {move["source"]} → {move["target"]}. One guest at a time.', now)
                    try:
                        params = {'target': move['target'], **bwlimit(current_config)}
                        if guest['type'] == 'qemu':
                            params.update({'online': int(guest['status'] == 'running'), 'with-local-disks': 0})
                        elif guest['status'] == 'running':
                            params['restart'] = 1
                        upid = await client.post(path, params)
                        if not isinstance(upid, str) or not upid.startswith('UPID:'):
                            raise ProxmoxError('No migration task ID returned. Check Proxmox before retrying.', uncertain=True)
                        with self.database() as db:
                            db.execute("UPDATE operation_tasks SET upid=?,status='running',message='Migration accepted by Proxmox.' WHERE id=?", (upid, task_id))
                    except ProxmoxError as exc:
                        with self.database() as db:
                            db.execute('UPDATE operation_tasks SET status=?,message=? WHERE id=?', ('unknown' if exc.uncertain else 'failed', str(exc), task_id))
                        if not exc.uncertain:
                            step = {**move, 'run_id': run_id, 'operation_id': operation_id}
                            self.continue_after_failure(run, step, str(exc), now)
                            return
                        raise
            except backup_guard.BackupBlocked as exc:
                with self.database() as db:
                    if scheduled_run:
                        cancel_backup(db, run, str(exc), now)
                    else:
                        update(db, run_id, 'backup_paused', str(exc), now, next_at=now + 15)
            except CandidateRejected as exc:
                if move and not exc.uncertain:
                    self.skip_candidate(run, move, str(exc), now)
                else:
                    self.finish(run_id, 'failed', str(exc), now)
            except ProxmoxError as exc:
                if exc.uncertain:
                    self.finish(run_id, 'failed', str(exc), now)
                elif automatic:
                    self.finish(run_id, 'cancelled', str(exc) + ' Waiting for a fresh controller assessment.', now)
                else:
                    with self.database() as db:
                        update(db, run_id, 'waiting', str(exc) + ' Retrying before further submissions.', now, next_at=now + 15)

    def skip_candidate(self, run, move, reason, now):
        with self.database() as db:
            preflight_failures.record(db, run['connection_id'], move, reason, now)
            db.execute('INSERT OR IGNORE INTO migration_run_exclusions VALUES(?,?,?,?)',
                       (run['id'], move['resource'], reason, now))
            automatic = run['trigger'] == 'continuous'
            update(db, run['id'], 'completed_with_errors' if automatic else 'waiting',
                   f'{move["resource"]} skipped: {reason}. Reassessing other guests and destinations.',
                   now, finish=automatic, next_at=None if automatic else now + 15)

    async def review_schedules(self, now=None):
        from . import scheduled
        now = time.time() if now is None else now
        async with self.lock:
            with self.database() as db:
                ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
            for connection_id in ids:
                try:
                    with self.database() as db:
                        scheduled.review(db, connection_id, now)
                except Exception:
                    logging.getLogger(__name__).exception('Scheduled balancing failed for connection %s.', connection_id)

    async def review_rules(self, now=None):
        now = time.time() if now is None else now
        async with self.lock:
            try:
                await background.run(self.review_rules_sync, now)
            except Exception:
                logging.getLogger(__name__).exception('Placement rule assessment failed; existing queues continue to be monitored.')

    def review_rules_sync(self, now):
        with self.database() as db:
            ids = [r[0] for r in db.execute('SELECT DISTINCT connection_id FROM placement_rules WHERE enabled=1')]
        for connection_id in ids:
            if now - self.rule_checks.get(connection_id, 0) < 15:
                continue
            self.rule_checks[connection_id] = now
            with self.database() as db:
                config = balancing.settings(db, connection_id)
                if config['paused'] or not config['placement_repair_enabled'] or active(db, connection_id) or backup_guard.read(db, connection_id, now)['blocked']:
                    continue
                try:
                    ensure_idle(db, connection_id)
                except ProxmoxError:
                    continue
                result = balancing.evaluate(db, connection_id, now, run_now=True, rule_only=True)
                if result['plan']:
                    start(db, connection_id, str(uuid4()), now, trigger='placement_rules', restart_containers=config['container_balancing_enabled'])

    async def review_native(self, now=None):
        from . import native_ha
        await native_ha.review(self, time.time() if now is None else now)

    async def tick(self):
        await self.review_schedules()
        await self.review_rules()
        await self.review_native()
        with self.database() as db:
            prune(db, time.time())
            ids = [r['id'] for r in db.execute('SELECT id FROM migration_runs WHERE finished_at IS NULL')]
        for run_id in ids:
            try:
                await self.step(run_id)
            except Exception:
                logging.getLogger(__name__).exception('Migration queue failed for run %s.', run_id)
                self.finish(run_id, 'failed', 'Unexpected queue error. Check Tasks & Events before retrying; no further migrations submitted.', time.time())

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).exception('Migration queue cycle failed; retrying automatically.')
            await asyncio.sleep(2)
