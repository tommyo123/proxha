import asyncio
import time

from .proxmox import ProxmoxError, segment
from .telemetry import numeric

MIB = 1024 ** 2
VERIFY_SECONDS = 120
SCHEMA = '''
CREATE TABLE IF NOT EXISTS balloon_requests (
    task_id INTEGER PRIMARY KEY REFERENCES operation_tasks(id) ON DELETE CASCADE,
    target_bytes INTEGER NOT NULL, started_at REAL NOT NULL
);
'''


def target(current):
    maximum, allocated = numeric(current.get('maxmem')), numeric(current.get('balloon'))
    if not maximum or maximum % MIB or not allocated or allocated > maximum:
        raise ProxmoxError('Free balloon requires valid live maximum RAM and balloon allocation. Check the VM balloon device and driver.')
    return int(maximum)


async def require_capacity(client, node, current):
    needed = target(current) - numeric(current.get('balloon'))
    host = await client.get(f'nodes/{segment(node)}/status')
    memory = host.get('memory', {}) if isinstance(host, dict) else {}
    memory = memory if isinstance(memory, dict) else {}
    total, used = numeric(memory.get('total')), numeric(memory.get('used'))
    if not total or used is None or used > total:
        raise ProxmoxError('Host memory capacity could not be verified for Free balloon.')
    reserve = max(512 * MIB, total * .02)
    if needed > 0 and needed > total - used - reserve:
        raise ProxmoxError('Not enough host RAM to free the entire balloon while retaining the host reserve (2% or 512 MiB). Free capacity or migrate the VM first.')


def accepted(response, redact):
    if not isinstance(response, str):
        raise ProxmoxError('Unexpected monitor response. Check the VM balloon allocation before retrying.', uncertain=True)
    if response.strip():
        raise ProxmoxError('Free balloon: ' + redact(response.strip()))


async def reconcile(database, connection_id, client):
    with database() as db:
        requests = [dict(row) for row in db.execute('''
            SELECT t.*, b.target_bytes, b.started_at FROM balloon_requests b
            JOIN operation_tasks t ON t.id=b.task_id JOIN operations o ON o.id=t.operation_id
            WHERE o.connection_id=? AND t.status='running'
        ''', (connection_id,))]
    semaphore = asyncio.Semaphore(4)

    async def verify(request):
        async with semaphore:
            expired = time.time() - request['started_at'] >= VERIFY_SECONDS
            status = 'running'
            try:
                current = await client.get(f'nodes/{segment(request["node"])}/qemu/{request["vmid"]}/status/current')
                if not isinstance(current, dict):
                    raise ProxmoxError('Invalid VM status response.')
                maximum, allocated = numeric(current.get('maxmem')), numeric(current.get('balloon'))
                if current.get('status') != 'running' or current.get('qmpstatus', 'running') != 'running':
                    status, message = 'failed', 'VM is no longer running; full balloon release could not be verified.'
                elif maximum != request['target_bytes']:
                    status, message = 'failed', 'VM maximum RAM changed; refresh the VM before requesting Free balloon again.'
                elif allocated is not None and allocated == maximum:
                    status, message = 'completed', f'Full RAM allocation confirmed: {maximum / MIB:g} MiB. Proxmox may reclaim RAM again under host pressure.'
                elif expired:
                    status, message = 'failed', 'Full RAM was not confirmed within 120 seconds. Check the balloon driver and host memory pressure. The command was not repeated.'
                else:
                    amount = f'{allocated / MIB:g}' if allocated is not None else 'unknown'
                    message = f'Free balloon requested; allocation {amount} / {request["target_bytes"] / MIB:g} MiB. Waiting for the guest driver.'
            except ProxmoxError as exc:
                status = 'unknown' if expired else 'running'
                message = f'Free balloon was requested, but allocation could not be verified: {exc}'
            with database() as db:
                db.execute("UPDATE operation_tasks SET status=?,message=? WHERE id=? AND status='running'",
                           (status, message, request['id']))

    await asyncio.gather(*(verify(request) for request in requests))
