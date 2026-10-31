import json
import time

from . import ha_guests, migration_failures, node_maintenance
from .proxmox import ProxmoxError, segment

MAX_FAILURES = 5
VERIFY_SECONDS = 120
# The HA manager starts the real migration after its hand-off task ends, so placement takes longer to confirm.
HA_VERIFY_SECONDS = 900


class PlacementPending(ProxmoxError):
    pass


async def read(client, step, expected, maintenance=False, ha_managed=False):
    if not step['upid']:
        raise ProxmoxError('Migration submission outcome is unknown. Check Proxmox tasks before retrying.')
    task_node = step['upid'].split(':')[1]
    result = await client.get(f'nodes/{segment(task_node)}/tasks/{segment(step["upid"])}/status')
    if result.get('status') == 'running':
        return 'running', 'Migration is running.'
    if result.get('status') != 'stopped' or not result.get('exitstatus'):
        raise ProxmoxError('Migration status is unknown. Check Proxmox tasks before retrying.')
    if result['exitstatus'] != 'OK':
        return 'failed', client.redact(str(result['exitstatus']))
    if ha_managed:
        status = await client.get('cluster/ha/status/current')
        if not ha_guests.stable(status, ha_guests.sid(step['resource']), step['target'], time.time()):
            raise PlacementPending('Waiting for the native HA manager to confirm this guest started on the target.')
    if maintenance:
        await node_maintenance.verify_inventory_access(client)
    try:
        resources = await client.get('cluster/resources')
        guest = next((r for r in resources if r.get('id') == step['resource']), None)
    except ProxmoxError as exc:
        if not exc.retryable:
            raise
        guest = None
    if guest and guest.get('node') == step['target'] and guest.get('status') == expected:
        return 'completed', 'OK'
    try:
        target = await client.get(f'nodes/{segment(step["target"])}/{step["resource"]}/status/current')
    except ProxmoxError as exc:
        if exc.retryable:
            raise
        if exc.status_code != 404:
            raise
        raise PlacementPending('Waiting for the guest to appear on the target.') from None
    if target.get('status') == expected and target.get('qmpstatus', expected) == expected:
        return 'completed', 'OK'
    raise PlacementPending('Waiting for the expected guest state on the target.')


def record(db, step, result, now):
    retry_at = None
    failures, since, verifying = step['poll_failures'], step['poll_error_since'], step['verify_started_at']
    if isinstance(result, PlacementPending):
        failures, since = 0, None
        verifying = now if verifying is None else verifying
        payload = db.execute('SELECT payload FROM operations WHERE id=?', (step['operation_id'],)).fetchone()
        limit = HA_VERIFY_SECONDS if payload and json.loads(payload['payload']).get('ha_managed') else VERIFY_SECONDS
        if now - verifying < limit:
            state, message, retry_at = 'verifying', str(result), now + 5
        else:
            state, message = 'unknown', f'Migration task finished, but guest placement and state could not be confirmed after {limit // 60} minutes. ' + str(result)
    elif isinstance(result, ProxmoxError):
        failures += 1
        since = now if since is None else since
        if result.retryable and failures < MAX_FAILURES and now - since < VERIFY_SECONDS:
            retry_at = now + min(30, 5 * 2 ** (failures - 1))
            state, message = 'status_retry', f'Status check failed ({failures}/{MAX_FAILURES}); retrying without resubmitting the migration. {result}'
        else:
            state, message = 'unknown', f'Migration status could not be verified. {result}'
    elif isinstance(result, Exception):
        state, message = 'unknown', 'Unexpected migration status response. Check server logs and Proxmox tasks.'
    else:
        state, message = result
        failures, since = 0, None
    terminal = state in ('completed', 'failed', 'unknown')
    db.execute('''UPDATE migration_run_steps SET poll_failures=?,poll_error_since=?,poll_retry_at=?,
        verify_started_at=?,settled_at=?,verified_at=? WHERE run_id=? AND resource=?''',
        (failures, since, retry_at, verifying, now if terminal else None, now if state == 'completed' else None, step['run_id'], step['resource']))
    # A retried check keeps the task in flight; only exhausted checks need acknowledgement.
    db.execute('UPDATE operation_tasks SET status=?,message=? WHERE operation_id=?',
               ('running' if retry_at else state, message, step['operation_id']))
    if state == 'failed':
        db.execute('''UPDATE native_tasks SET status='failed',message=?,finished_at=COALESCE(finished_at,?)
            WHERE connection_id=(SELECT connection_id FROM migration_runs WHERE id=?)
            AND upid=(SELECT upid FROM operation_tasks WHERE operation_id=?)''',
            (message, now, step['run_id'], step['operation_id']))
        migration_failures.record(db, step, message, now)
    return state, message
