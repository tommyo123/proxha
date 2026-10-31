import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from . import backup_guard, balancing, migration_failures, migration_groups, migration_poll, migration_queue as queue, node_maintenance, placement_rules
from .proxmox import ProxmoxError, segment
from . import native_ha, infrastructure

RETRY_SECONDS = 15


def wait_for_admission(db, run, blockers, now):
    message = 'Waiting for capacity or fresh measurements. Automatic retry every 15 seconds; Stop evacuation cancels further moves.'
    queue.update(db, run['id'], 'waiting', message, now, next_at=now + RETRY_SECONDS)
    node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'waiting', message, now, blockers)


def steps(db, run_id):
    return [dict(r) for r in db.execute('''SELECT s.*,t.id AS task_id,t.status,t.message,t.upid,t.node,o.payload
        FROM migration_run_steps s LEFT JOIN operation_tasks t ON t.operation_id=s.operation_id
        LEFT JOIN operations o ON o.id=s.operation_id WHERE s.run_id=? ORDER BY s.created_at,s.resource''', (run_id,))]


def halt(db, run, message, now):
    db.execute('UPDATE migration_runs SET stop_requested=1,failure_message=COALESCE(failure_message,?) WHERE id=?', (message, run['id']))
    pending = any(s['settled_at'] is None for s in steps(db, run['id']))
    queue.update(db, run['id'], 'stopping' if pending else 'failed', message + (' Waiting for submitted migrations to finish.' if pending else ''), now, finish=not pending)
    node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'blocked', message, now)


async def poll(worker, client, run, now):
    with worker.database() as db:
        pending = [s for s in steps(db, run['id']) if s['settled_at'] is None]
    if not pending:
        return

    due = [s for s in pending if not s['poll_retry_at'] or now >= s['poll_retry_at']]
    results = await asyncio.gather(*(migration_poll.read(client, step,
        json.loads(step['payload']).get('expected_status', 'running'), maintenance=True) for step in due), return_exceptions=True)
    for step, result in zip(due, results):
        with worker.database() as db:
            state, message = migration_poll.record(db, step, result, now)
            if state in ('running', 'status_retry', 'verifying'):
                continue
            if state == 'completed':
                db.execute('UPDATE migration_runs SET next_at=NULL WHERE id=?', (run['id'],))
                db.execute('INSERT OR IGNORE INTO placement_events VALUES(?,?,?,?,?,?)',
                           (run['connection_id'], step['resource'], now, step['created_at'], step['source'], step['target']))
            elif state == 'failed':
                queue.update(db, run['id'], 'migrating',
                             f'{step["resource"]} failed and was skipped. Failure penalty: 200 points. Continuing with other guests.', now)
            else:
                halt(db, run, f'{step["resource"]}: {message}', now)


async def submit(worker, client, run, move, reservations, now):
    allowed = {s['upid'] for s in reservations if s['upid']}
    with worker.database() as db:
        host_performance = balancing.performance(db, run['connection_id'])
        infrastructure_status = infrastructure.latest(db, run['connection_id'], now)
    guest, nodes, path = await queue.live_preflight(client, move, maintenance=True, restart_containers=bool(run['restart_containers']), allowed_upids=allowed, host_performance=host_performance, infrastructure_status=infrastructure_status)
    with worker.database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = queue.document(db, run['connection_id'], run['id'])
        config = balancing.settings(db, run['connection_id'])
        if not current['active'] or current['stop_requested']:
            return False
        if config['paused']:
            raise ProxmoxError('Planning was paused before submission.')
        if sum(s['settled_at'] is None for s in current['steps']) >= config['maintenance_concurrency']:
            return False
        if queue.target_slots(current['steps'], move['target'], config) <= 0:
            return False
        queue.ensure_idle(db, run['connection_id'], run['id'])
        _, overrides = await placement_rules.live_check(client, db, run['connection_id'], {move['resource']: move['target']}, maintenance=True)
        excluded = node_maintenance.excluded(db, run['connection_id'])
        if move['source'] not in excluded or move['target'] in excluded:
            raise ProxmoxError('Host maintenance policy changed before submission.')
        policy = migration_groups.pair_policy(migration_groups.list_groups(db, run['connection_id']), nodes, move['source'], move['target'])
        if not policy['allowed']:
            raise ProxmoxError(policy['reason'])
        if time.time() - now > 45:
            raise queue.MigrationDeferred('Preflight took too long; waiting to refresh capacity evidence.')
        reason = infrastructure.latest(db, run['connection_id'], time.time()).get(move['target'], {}).get('block_reason')
        if reason:
            raise queue.MigrationDeferred(reason)
        operation_id = str(uuid4())
        payload = {'request_id': operation_id, 'action': 'migrate', 'vmids': [guest['vmid']], 'target': move['target'],
                   'with_local_disks': False, 'restart_containers': bool(run['restart_containers']), 'migration_run_id': run['id'],
                   'maintenance_node': run['maintenance_node'], 'expected_status': guest['status'],
                   'memory_mode': move.get('memory_mode', 'configured'), 'reserved_memory': move['reserved_memory'],
                   'configured_memory': move.get('configured_memory'), 'resident_memory': move.get('resident_memory'), 'placement_overrides': overrides}
        db.execute('INSERT INTO operations VALUES(?,?,?,?)', (operation_id, run['connection_id'], json.dumps(payload), datetime.fromtimestamp(now, timezone.utc).isoformat()))
        task_id = db.execute('INSERT INTO operation_tasks(operation_id,vmid,name,node,kind,status,message) VALUES(?,?,?,?,?,?,?)',
                             (operation_id, guest['vmid'], guest.get('name'), move['source'], guest['type'], 'submitting', 'Submitting evacuation migration.')).lastrowid
        db.execute('''INSERT INTO migration_run_steps(run_id,resource,source,target,score,operation_id,created_at,reserved_memory,reserved_cpu_cores)
            VALUES(?,?,?,?,?,?,?,?,?)''', (run['id'], move['resource'], move['source'], move['target'], move['score'], operation_id, now, move['reserved_memory'], move['reserved_cpu_cores']))
        queue.update(db, run['id'], 'migrating', 'Evacuating guests with reserved destination capacity.', now)
        node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'evacuating', 'Evacuating guests with the configured parallel limit.', now)
    try:
        params = {'target': move['target'], **queue.bwlimit(config)}
        if guest['type'] == 'qemu':
            params.update(online=int(guest['status'] == 'running'), **{'with-local-disks': 0})
        elif guest['status'] == 'running':
            params['restart'] = 1
        upid = await client.post(path, params)
        if not isinstance(upid, str) or not upid.startswith('UPID:'):
            raise ProxmoxError('No migration task ID returned. Check Proxmox before retrying.', uncertain=True)
        with worker.database() as db:
            db.execute("UPDATE operation_tasks SET upid=?,status='running',message='Migration accepted by Proxmox.' WHERE id=?", (upid, task_id))
    except ProxmoxError as exc:
        safe_failure = not exc.uncertain
        with worker.database() as db:
            db.execute('UPDATE operation_tasks SET status=?,message=? WHERE id=?', ('failed' if safe_failure else 'unknown', str(exc), task_id))
            db.execute('UPDATE migration_run_steps SET settled_at=? WHERE run_id=? AND resource=?', (now, run['id'], move['resource']))
            if safe_failure:
                migration_failures.record(db, {**move, 'run_id': run['id'], 'operation_id': operation_id}, str(exc), now)
                queue.update(db, run['id'], 'waiting',
                             f'{move["resource"]} submission failed and was skipped. Failure penalty: 200 points. Continuing with other guests.',
                             now, next_at=now + RETRY_SECONDS)
        if safe_failure:
            return True
        raise
    return True


async def step(worker, run, now):
    try:
        async with worker.client_factory(worker.config_for(run['connection_id'])) as client:
            await poll(worker, client, run, now)
            for _ in range(11):
                with worker.database() as db:
                    current = queue.document(db, run['connection_id'], run['id'])
                    if not current['active']:
                        return
                    config = balancing.settings(db, run['connection_id'])
                    reservations = steps(db, run['id'])
                    pending = sum(s['settled_at'] is None for s in reservations)
                    if current['stop_requested'] or config['paused']:
                        db.execute('UPDATE migration_runs SET stop_requested=1 WHERE id=?', (run['id'],))
                        message = current['failure_message'] or 'Evacuation stopped. Host remains excluded from placement.'
                        queue.update(db, run['id'], 'stopping' if pending else 'failed' if current['failure_message'] else 'stopped',
                                     message + (f' Waiting for {pending} submitted migrations.' if pending else ''), now, finish=not pending)
                        if not pending:
                            node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'blocked', message, now)
                        return
                    retrying = [s for s in reservations if s['settled_at'] is None and s['poll_retry_at']]
                    if retrying:
                        queue.update(db, run['id'], 'status_retry', 'Waiting for submitted migrations to be verified. New submissions are paused.', now)
                        return
                    limit = config['maintenance_concurrency']
                    if pending >= limit:
                        queue.update(db, run['id'], 'migrating', f'{pending} migrations in flight; configured limit {limit}. A free slot will be refilled after fresh checks.', now)
                        return
                    if current['next_at'] and now < current['next_at']:
                        return
                    if run['maintenance_node'] not in node_maintenance.excluded(db, run['connection_id']):
                        raise ProxmoxError('Maintenance exclusion was removed; evacuation stopped.')
                    await backup_guard.require_clear(client)
                    try:
                        native_ready = await native_ha.ensure_maintenance(client, db, run['connection_id'], run['maintenance_node'], now, bool(run['restart_containers']))
                    except native_ha.Blocked as exc:
                        if pending:
                            queue.update(db, run['id'], 'waiting', f'{exc} Waiting for {pending} submitted migrations.', now, next_at=now + RETRY_SECONDS)
                            node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'waiting', str(exc), now, exc.blockers)
                        else:
                            queue.update(db, run['id'], 'blocked', str(exc), now, finish=True)
                            node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'blocked', str(exc), now, exc.blockers)
                        return
                    except native_ha.Waiting as exc:
                        queue.update(db, run['id'], 'waiting', str(exc), now, next_at=now + RETRY_SECONDS)
                        node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'waiting', str(exc), now)
                        return
                    except ProxmoxError as exc:
                        if not exc.retryable or exc.uncertain:
                            raise
                        wait_for_admission(db, run, [{'resource': f'node/{run["maintenance_node"]}', 'reason': f'Native HA status unavailable: {exc}', 'retryable': True}], now)
                        return
                    if not native_ready:
                        message = 'Waiting for native HA evacuation and maintenance acknowledgement. Native HA controls its own parallelism.'
                        queue.update(db, run['id'], 'waiting', message, now, next_at=now + RETRY_SECONDS)
                        node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'waiting', message, now)
                        return
                    queue.ensure_idle(db, run['connection_id'], run['id'])
                    result = await node_maintenance.evacuation_plan(client, db, run['connection_id'], run['maintenance_node'], now, bool(run['restart_containers']), reservations)
                    if not result['moves']:
                        for guest in result['remaining']:
                            if guest['resource'] in {s['resource'] for s in reservations if s['verified_at'] is not None}:
                                result['blockers'].append({'resource': guest['resource'], 'reason': 'Guest returned after evacuation. Review placement before resuming.', 'retryable': False})
                        if pending or any(b.get('retryable') for b in result['blockers']):
                            wait_for_admission(db, run, result['blockers'], now)
                            return
                        blocked = bool(result['remaining'])
                        message = 'Manual action is required for the remaining guests. See admission details below.' if blocked else 'Evacuation complete. Host is empty and excluded until Exit maintenance.'
                        queue.update(db, run['id'], 'blocked' if blocked else 'completed', message, now, finish=True)
                        node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'blocked' if blocked else 'maintenance', message, now, result['blockers'])
                        return
                    move = result['moves'][0]
                try:
                    if not await submit(worker, client, run, move, reservations, now):
                        return
                except queue.MigrationDeferred as exc:
                    blocker = {'resource': move['resource'], 'name': move.get('name'), 'reason': f'{move["target"]}: {exc}', 'retryable': True,
                               'targets': [{'node': move['target'], 'reason': str(exc), 'retryable': True, 'capacity': exc.capacity}]}
                    with worker.database() as db:
                        current = queue.document(db, run['connection_id'], run['id'])
                        if current['active'] and not current['stop_requested']:
                            wait_for_admission(db, run, [blocker, *result['blockers']], now)
                    return
    except backup_guard.BackupBlocked as exc:
        with worker.database() as db:
            queue.update(db, run['id'], 'backup_paused', str(exc), now, next_at=now + RETRY_SECONDS)
            node_maintenance.set_state(db, run['connection_id'], run['maintenance_node'], 'waiting', str(exc), now)
    except ProxmoxError as exc:
        with worker.database() as db:
            halt(db, run, str(exc), now)
