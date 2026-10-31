import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from . import infrastructure, adaptive, backup_guard, balancing, guest_policy, migration_failures, migration_groups
from . import migration_poll, migration_queue as queue, node_maintenance, placement_repair, placement_rules
from .proxmox import ProxmoxError, segment


class PlanChanged(ProxmoxError):
    pass


def current(db, run_id):
    row = db.execute('SELECT * FROM placement_batches WHERE run_id=? AND finished_at IS NULL', (run_id,)).fetchone()
    return {**dict(row), 'document': json.loads(row['document'])} if row else None


def steps(db, run_id, plan):
    resources = {m['resource'] for m in plan['unit_moves']}
    return [dict(r) for r in db.execute('''SELECT s.*,t.id AS task_id,t.status,t.message,t.upid,t.node,o.payload
        FROM migration_run_steps s JOIN operation_tasks t ON t.operation_id=s.operation_id
        JOIN operations o ON o.id=s.operation_id WHERE s.run_id=?''', (run_id,))
        if r['resource'] in resources]


def finish(db, run, batch, now, status, message):
    db.execute('UPDATE placement_batches SET finished_at=?,status=?,message=? WHERE id=?', (now, status, message, batch['id']))
    if status == 'completed':
        placement_rules.clear_failures(db, run['connection_id'], batch['document']['unit_key'])
    ended = run['stop_requested'] or status in ('failed', 'stopped', 'cancelled_backup') or run['trigger'] in ('continuous', 'placement_rules')
    final = 'stopped' if run['stop_requested'] and status not in ('cancelled_backup', 'failed') else status
    queue.update(db, run['id'], final if ended else 'settling', message, now,
                 finish=ended, next_at=None if ended else now + queue.SETTLE_SECONDS)


async def poll(worker, client, run, plan, now):
    with worker.database() as db:
        due = [s for s in steps(db, run['id'], plan) if s['settled_at'] is None and (s['poll_retry_at'] or 0) <= now]
    results = await asyncio.gather(*(migration_poll.read(client, s, 'running', ha_managed=json.loads(s['payload']).get('ha_managed', False)) for s in due), return_exceptions=True)
    with worker.database() as db:
        for step, result in zip(due, results):
            status, message = migration_poll.record(db, step, result, now)
            if status == 'completed':
                db.execute('INSERT OR IGNORE INTO placement_events VALUES(?,?,?,?,?,?)',
                           (run['connection_id'], step['resource'], now, step['created_at'], step['source'], step['target']))
            elif status == 'unknown':
                db.execute('UPDATE migration_runs SET stop_requested=1,failure_message=? WHERE id=?', (message, run['id']))


async def submit(worker, client, run, move, prepared, now, cohort=None):
    guest, _, path = prepared
    recorded = []
    with worker.database() as db:
        reason = infrastructure.latest(db, run['connection_id'], queue.time.time()).get(move['target'], {}).get('block_reason')
        if reason:
            raise queue.MigrationDeferred(reason)
        bandwidth = queue.bwlimit(balancing.settings(db, run['connection_id']))
        for member, (item, _, _) in cohort or [(move, prepared)]:
            operation_id = str(uuid4())
            payload = {'request_id': operation_id, 'action': 'migrate', 'vmids': [item['vmid']], 'target': member['target'],
                       'with_local_disks': False, 'restart_containers': bool(run['restart_containers']),
                       'migration_run_id': run['id'], 'expected_status': 'running', 'trigger': run['trigger'],
                       'score': member['score'], 'placement_unit': member['unit_key'],
                       'ha_managed': bool(member.get('ha_managed')), 'ha_cohort': member.get('ha_cohort', []),
                       'placement_overrides': member.get('placement_overrides', [])}
            db.execute('INSERT INTO operations VALUES(?,?,?,?)',
                       (operation_id, run['connection_id'], json.dumps(payload), datetime.fromtimestamp(now, timezone.utc).isoformat()))
            task_id = db.execute('INSERT INTO operation_tasks(operation_id,vmid,name,node,kind,status,message) VALUES(?,?,?,?,?,?,?)',
                                (operation_id, item['vmid'], item.get('name'), member['source'], item['type'], 'submitting', 'Submitting coordinated group migration.')).lastrowid
            db.execute('INSERT INTO migration_run_steps(run_id,resource,source,target,score,operation_id,created_at,reserved_memory,reserved_cpu_cores) VALUES(?,?,?,?,?,?,?,?,?)',
                       (run['id'], member['resource'], member['source'], member['target'], member['score'], operation_id,
                        now, member['reserved_memory'], member['reserved_cpu_cores']))
            recorded.append((member, operation_id, task_id))
    try:
        params = {'target': move['target'], **bandwidth, **({'online': 1, 'with-local-disks': 0} if guest['type'] == 'qemu' else {'restart': 1})}
        upid = await client.post(path, params)
        if not isinstance(upid, str) or not upid.startswith('UPID:'):
            raise ProxmoxError('No task ID returned. Check Proxmox before retrying.', uncertain=True)
        with worker.database() as db:
            for _, _, task_id in recorded:
                db.execute("UPDATE operation_tasks SET upid=?,status='running',message='Coordinated migration accepted.' WHERE id=?", (upid, task_id))
    except ProxmoxError as exc:
        with worker.database() as db:
            for member, operation_id, task_id in recorded:
                db.execute('UPDATE operation_tasks SET status=?,message=? WHERE id=?', ('unknown' if exc.uncertain else 'failed', str(exc), task_id))
                db.execute('UPDATE migration_run_steps SET settled_at=? WHERE operation_id=?', (now, operation_id))
                if exc.uncertain:
                    db.execute('UPDATE migration_runs SET stop_requested=1,failure_message=? WHERE id=?', (str(exc), run['id']))
                else:
                    migration_failures.record(db, {**member, 'run_id': run['id'], 'operation_id': operation_id}, str(exc), now)


async def step(worker, run, now, move=None):
    with worker.database() as db:
        if move:
            db.execute('INSERT INTO placement_batches(run_id,document,created_at) VALUES(?,?,?)', (run['id'], json.dumps(move), now))
            db.execute('INSERT OR REPLACE INTO placement_intents VALUES(?,?,?,?,?)',
                       (run['connection_id'], move['unit_key'], move['placement_policy_key'], move['target'], now))
        batch = current(db, run['id'])
    if not batch:
        return
    plan = batch['document']
    async with worker.client_factory(worker.config_for(run['connection_id'])) as client:
        await poll(worker, client, run, plan, now)
        with worker.database() as db:
            run = queue.document(db, run['connection_id'], run['id'])
            submitted = steps(db, run['id'], plan)
            config = balancing.settings(db, run['connection_id'])
        pending = [s for s in submitted if s['settled_at'] is None]
        remaining = [m for m in plan['unit_moves'] if m['resource'] not in {s['resource'] for s in submitted}]
        if any(s['status'] == 'failed' for s in submitted):
            with worker.database() as db:
                for member in remaining:
                    db.execute('INSERT OR IGNORE INTO migration_run_exclusions VALUES(?,?,?,?)',
                               (run['id'], member['resource'], 'A group member failed; retain the target and reassess the complete group.', now))
            remaining = []
        if run['stop_requested'] or not queue.enabled(run, config):
            with worker.database() as db:
                if pending:
                    queue.update(db, run['id'], 'stopping', 'Waiting for submitted group migrations; no new guests will move.', now)
                else:
                    status = 'cancelled_backup' if run['status'] == 'cancelling_backup' else 'failed' if run['failure_message'] else 'stopped'
                    finish(db, run, batch, now, status, run['failure_message'] or 'Group migration stopped. No rollback migrations were submitted.')
            return
        if not remaining and not pending:
            with worker.database() as db:
                failed = any(s['status'] == 'failed' for s in submitted)
                finish(db, run, batch, now, 'completed_with_errors' if failed else 'completed',
                       'Group placement finished. Failed members retain the same repair target; no rollback is performed.' if failed else
                       'Group placement verified. Waiting for fresh measurements before another placement decision.')
            return
        try:
            await backup_guard.require_clear(client)
            if any(s['poll_retry_at'] for s in pending):
                return
            limit = queue.target_limit(config, config['placement_concurrency'])
            if len(pending) >= limit or not remaining:
                return
            with worker.database() as db:
                queue.ensure_idle(db, run['connection_id'], run['id'])
                policy, overrides = await placement_rules.live_check(client, db, run['connection_id'], dict.fromkeys(plan['unit_members'], plan['target']))
                if policy.key != plan['placement_policy_key']:
                    raise PlanChanged('Placement rules or group membership changed; discard this group plan and reassess.')
                if plan.get('rule_repair') and policy.violations({**policy.placements, **dict.fromkeys(plan['unit_members'], plan['target'])}) >= policy.violations():
                    raise PlanChanged('The proposed move no longer repairs a placement rule; reassess before migrating.')
                for resource in plan['unit_members']:
                    guest = policy.guests.get(resource)
                    original = next((m['source'] for m in plan['unit_moves'] if m['resource'] == resource), plan['target'])
                    if not guest or guest['node'] not in (original, plan['target']):
                        raise PlanChanged('A group member changed running state or moved outside the planned hosts.')
                if plan['target'] not in policy.available:
                    raise ProxmoxError('The group target is offline or in maintenance.')
                if run['trigger'] == 'continuous' and not plan.get('rule_repair') and plan['urgency'] != 'critical':
                    budget = adaptive.hourly_budget(db, run['connection_id'], now, config['continuous_migrations_per_hour'])
                    if budget['remaining'] < len(remaining):
                        raise PlanChanged('The continuous hourly budget cannot admit the complete group.')
            # Validate every remaining member and reserve the complete incoming group before a wave starts.
            prepared = []
            for member in remaining:
                member = {**member, 'unit_key': plan['unit_key'], 'placement_overrides': overrides}
                others = [m for m in remaining if m['resource'] != member['resource']]
                member['pending_memory'] = sum(m['reserved_memory'] for m in others) + sum(s['reserved_memory'] for s in pending)
                member['pending_cpu_cores'] = sum(m['reserved_cpu_cores'] for m in others) + sum(s['reserved_cpu_cores'] for s in pending)
                with worker.database() as db:
                    host_performance = balancing.performance(db, run['connection_id'])
                    infrastructure_status = infrastructure.latest(db, run['connection_id'], now)
                queue.ensure_scope(run, member['resource'])
                checked = await queue.live_preflight(client, member, restart_containers=bool(run['restart_containers']),
                                                     allowed_upids={s['upid'] for s in submitted if s['upid']}, host_performance=host_performance,
                                                     ha_allowed=config['ha_guests_enabled'], ha_unit=plan['unit_members'], infrastructure_status=infrastructure_status)
                guest, nodes, _ = checked
                with worker.database() as db:
                    boundary = migration_groups.automatic_migration_policy(migration_groups.list_groups(db, run['connection_id']), nodes, guest, member['target'],
                        restart_containers=bool(run['restart_containers']), stickiness=guest_policy.read(db, run['connection_id'], member['resource'])['stickiness'],
                        intent='placement_rules' if plan.get('rule_repair') else 'balance',
                        exception_reason='Live placement rule violation verified.' if plan.get('rule_repair') else None)
                    if not boundary['allowed'] or member['source'] in node_maintenance.excluded(db, run['connection_id']):
                        raise ProxmoxError(boundary['reason'] if not boundary['allowed'] else 'Source host entered maintenance.')
                    if boundary.get('code') == 'stickiness_override':
                        member['placement_overrides'] = [*member['placement_overrides'], f'{member["resource"]}: {boundary["reason"]}']
                prepared.append((member, checked))
            if queue.time.time() - now > 45:
                raise PlanChanged('Group preflight expired. Reassess before submitting migrations.')
            await backup_guard.require_clear(client)
            with worker.database() as db:
                final_policy, _ = await placement_rules.live_check(client, db, run['connection_id'], dict.fromkeys(plan['unit_members'], plan['target']))
                if final_policy.key != plan['placement_policy_key']:
                    raise PlanChanged('Placement policy changed during preflight.')
                if plan.get('rule_repair') and final_policy.violations({**final_policy.placements, **dict.fromkeys(plan['unit_members'], plan['target'])}) >= final_policy.violations():
                    raise PlanChanged('Placement rules were repaired during preflight; discard the obsolete move.')
                if any(final_policy.guests.get(m['resource'], {}).get('node') != m['source'] for m, _ in prepared):
                    raise PlanChanged('Guest placement changed during group preflight.')
            target = await client.get(f'nodes/{segment(plan["target"])}/status')
            readings = (target.get('memory', {}).get('total'), target.get('memory', {}).get('used'),
                        target.get('cpuinfo', {}).get('cpus'), target.get('cpu'))
            if any(not isinstance(v, (int, float)) or v < 0 for v in readings) or not readings[0] or not readings[2]:
                raise queue.MigrationDeferred('Target capacity readings are incomplete.')
            budget = node_maintenance.capacity_budget(target['memory']['total'], target['memory']['used'],
                target['cpuinfo']['cpus'], target['cpu'], sum(m['reserved_memory'] for m, _ in prepared),
                sum(m['reserved_cpu_cores'] for m, _ in prepared), sum(s['reserved_memory'] for s in pending),
                sum(s['reserved_cpu_cores'] for s in pending), 'configured')
            reason = (placement_repair.capacity_reason(readings[0], readings[1], sum(m['reserved_memory'] for m, _ in prepared),
                      sum(s['reserved_memory'] for s in pending), readings[2], readings[3],
                      sum(m['reserved_cpu_cores'] for m, _ in prepared), sum(s['reserved_cpu_cores'] for s in pending))
                      if plan.get('rule_repair') else node_maintenance.capacity_reason(budget))
            if reason:
                raise queue.MigrationDeferred(reason, budget)
            with worker.database() as db:
                queue.update(db, run['id'], 'migrating', f'Moving the group to {plan["target"]}. Parallel limit: {config["placement_concurrency"]}.', now)
            sent, available = set(), limit - len(pending)
            for member, checked in prepared:
                if member['resource'] in sent:
                    continue
                peers = set(member.get('ha_cohort', []))
                cohort = [(m, c) for m, c in prepared if m['resource'] in peers] if peers else [(member, checked)]
                if len(cohort) > available:
                    if not pending and not sent:
                        raise PlanChanged('Increase group migration concurrency to fit the native HA affinity group.')
                    break
                with worker.database() as db:
                    if queue.document(db, run['connection_id'], run['id'])['stop_requested'] or not queue.enabled(run, balancing.settings(db, run['connection_id'])):
                        db.execute('UPDATE migration_runs SET stop_requested=1 WHERE id=?', (run['id'],))
                        break
                await backup_guard.require_clear(client)
                await submit(worker, client, run, member, checked, now, cohort)
                sent.update(m['resource'] for m, _ in cohort)
                available -= len(cohort)
        except backup_guard.BackupBlocked as exc:
            with worker.database() as db:
                if run['trigger'] in ('scheduled', 'aggressive_manual'):
                    queue.cancel_backup(db, run, str(exc), now)
                else:
                    queue.update(db, run['id'], 'backup_paused', str(exc), now, next_at=now + 15)
        except ProxmoxError as exc:
            with worker.database() as db:
                for member in remaining:
                    db.execute('INSERT OR IGNORE INTO migration_run_exclusions VALUES(?,?,?,?)', (run['id'], member['resource'], str(exc), now))
                if not isinstance(exc, (PlanChanged, queue.QueueBusy)):
                    # Back off this unit and target so live-only blockers are not retried every cycle.
                    placement_rules.record_failure(db, run['connection_id'], plan['unit_key'], plan['target'], str(exc), now)
                if pending:
                    db.execute('UPDATE migration_runs SET stop_requested=1,failure_message=? WHERE id=?', (str(exc), run['id']))
                    queue.update(db, run['id'], 'stopping', str(exc) + ' Waiting for accepted tasks.', now)
                else:
                    finish(db, run, batch, now, 'cancelled', str(exc) + ' The group plan was discarded; other candidates can be reassessed.')
