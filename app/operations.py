import asyncio
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .proxmox import ProxmoxError, segment
from . import backup_guard, free_balloon


class OperationInput(BaseModel):
    request_id: UUID
    action: Literal['start', 'shutdown', 'stop', 'reset', 'reboot', 'hibernate', 'migrate', 'free-balloon']
    vmids: list[int] = Field(min_length=1, max_length=100)
    target: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.-]*$')
    with_local_disks: bool = False
    restart_containers: bool = False

    @field_validator('vmids')
    @classmethod
    def validate_ids(cls, ids):
        if any(i < 100 or i > 999999999 for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Guest IDs must be unique and within the Proxmox VMID range.')
        return ids


def action_request(operation, guest, current):
    action, kind, vmid = operation.action, guest['type'], guest['vmid']
    if guest.get('template'):
        raise ProxmoxError(f'{vmid}: templates cannot be operated on here.')
    if current.get('lock') or guest.get('lock'):
        raise ProxmoxError(f'{vmid}: guest is locked by another operation.')
    if current.get('status') not in ('running', 'stopped'):
        raise ProxmoxError(f'{vmid}: current guest state is unavailable.')
    if kind == 'lxc' and action in ('reset', 'hibernate', 'free-balloon'):
        raise ProxmoxError(f'{vmid}: {action} is only supported for QEMU VMs.')
    running = current.get('status') == 'running'
    if action == 'start' and current.get('status') != 'stopped':
        raise ProxmoxError(f'{vmid}: start requires a stopped guest.')
    if action not in ('start', 'migrate') and not running:
        raise ProxmoxError(f'{vmid}: {action} requires a running guest.')
    if action in ('shutdown', 'reboot', 'hibernate', 'free-balloon') and current.get('qmpstatus') in ('paused', 'suspended'):
        raise ProxmoxError(f'{vmid}: guest is paused; resume it in Proxmox first.')
    if action == 'hibernate' and current.get('ha', {}).get('managed'):
        raise ProxmoxError(f'{vmid}: HA-managed VMs cannot hibernate.')
    base = f'nodes/{segment(guest["node"])}/{kind}/{vmid}'
    if action == 'free-balloon':
        if current.get('qmpstatus', 'running') != 'running':
            raise ProxmoxError(f'{vmid}: Free balloon requires a running VM.')
        return f'{base}/monitor', {'command': f'balloon {free_balloon.target(current) // free_balloon.MIB}'}
    if action == 'migrate':
        if not operation.target or operation.target == guest['node']:
            raise ProxmoxError(f'{vmid}: choose a different target node.')
        params = {'target': operation.target}
        if kind == 'qemu':
            params.update({'online': int(running), 'with-local-disks': int(operation.with_local_disks)})
        elif running:
            if not operation.restart_containers:
                raise ProxmoxError(f'{vmid}: running containers require restart migration with downtime.')
            params['restart'] = 1
        return f'{base}/migrate', params
    if action == 'hibernate':
        return f'{base}/status/suspend', {'todisk': 1}
    return f'{base}/status/{action}', {}


async def preflight(client, operation):
    if operation.action == 'free-balloon' and len(operation.vmids) != 1:
        raise ProxmoxError('Select one VM at a time for Free balloon.')
    resources, cluster_status = await asyncio.gather(client.get('cluster/resources'), client.get('cluster/status'))
    if operation.action == 'migrate':
        await backup_guard.require_clear(client, resources)
    if any(s.get('type') == 'cluster' and s.get('quorate') == 0 for s in cluster_status):
        raise ProxmoxError('Cluster has no quorum.')
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    guests = {r['vmid']: r for r in resources if r.get('type') in ('qemu', 'lxc')}
    if operation.action == 'migrate' and (operation.target not in nodes or nodes[operation.target].get('status') != 'online'):
        raise ProxmoxError('Migration target must be an online node in this cluster.')
    prepared = []
    for vmid in operation.vmids:
        guest = guests.get(vmid)
        if not guest:
            raise ProxmoxError(f'{vmid}: guest is no longer visible in this cluster.')
        if nodes.get(guest['node'], {}).get('status') != 'online':
            raise ProxmoxError(f'{vmid}: source node is offline or not visible.')
        current = await client.get(f'nodes/{segment(guest["node"])}/{guest["type"]}/{vmid}/status/current')
        path, params = action_request(operation, guest, current)
        if operation.action == 'free-balloon':
            await free_balloon.require_capacity(client, guest['node'], current)
        prepared.append((guest, path, params))
    return prepared
