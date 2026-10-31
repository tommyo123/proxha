import asyncio
import json
import re
import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from . import native_ha, storage, telemetry
from .proxmox import ProxmoxError, segment

SCHEMA = '''
CREATE TABLE IF NOT EXISTS node_maintenance (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, enabled INTEGER NOT NULL, state TEXT NOT NULL,
    message TEXT NOT NULL, updated_at REAL NOT NULL, run_id TEXT,
    blockers TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(connection_id,node)
);
CREATE TABLE IF NOT EXISTS node_power_actions (
    id TEXT PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, uptime REAL
);
'''


class EnterInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    restart_containers: bool = Field(default=True, strict=True)


class PowerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    command: Literal['reboot', 'shutdown']
    confirm_node: str


def excluded(db, connection_id):
    return {r['node'] for r in db.execute('SELECT node FROM node_maintenance WHERE connection_id=? AND enabled=1', (connection_id,))}


def read(db, connection_id, node):
    row = db.execute('SELECT * FROM node_maintenance WHERE connection_id=? AND node=?', (connection_id, node)).fetchone()
    result = dict(row) if row else {'node': node, 'enabled': False, 'state': 'normal', 'message': 'Available for placement.', 'run_id': None, 'updated_at': None, 'blockers': '[]'}
    result['enabled'] = bool(result['enabled'])
    result['blockers'] = json.loads(result['blockers'])
    power = db.execute('SELECT * FROM node_power_actions WHERE connection_id=? AND node=? ORDER BY created_at DESC LIMIT 1', (connection_id, node)).fetchone()
    result['power_action'] = dict(power) if power else None
    result['native_ha'] = native_ha.read(db, connection_id, node)
    result['scope'] = 'Native HA evacuates its resources first; ProxHA evacuates the remaining guests.'
    return result


def set_state(db, connection_id, node, state, message, now, blockers=None):
    db.execute('UPDATE node_maintenance SET state=?,message=?,updated_at=?,blockers=? WHERE connection_id=? AND node=? AND enabled=1',
               (state, message, now, json.dumps(blockers or []), connection_id, node))


def residents(resources, node):
    return [r for r in resources if r.get('type') in ('qemu', 'lxc') and r.get('node') == node]


def native_maintenance(row):
    return row.get('mode') == 'maintenance' or row.get('maintenance') in (1, True) or bool(re.search(r'\bmaintenance\b', str(row.get('status', ''))))


async def verify_inventory_access(client):
    permissions = await client.get('access/permissions')
    required = {'/': 'Sys.Audit', '/access': 'Sys.Audit', '/vms': 'VM.Audit', '/nodes': 'Sys.Audit'}
    if any(permissions.get(path, {}).get(privilege) != 1 for path, privilege in required.items()):
        raise ProxmoxError('Maintenance requires inherited VM.Audit on /vms and Sys.Audit on /, /access and /nodes to verify the complete inventory.')
    # Empty effective paths are omitted by Proxmox; ACL paths reveal NoAccess overrides.
    acl = await client.get('access/acl')
    acl_paths = {row['path'] for row in acl}
    paths = set(permissions) | acl_paths
    for path in paths:
        privilege = 'VM.Audit' if re.match(r'^/vms(/|$)', path) or path in acl_paths and re.match(r'^/pool(/|$)', path) else 'Sys.Audit' if re.match(r'^/nodes(/|$)', path) else None
        if privilege and privilege not in permissions.get(path, {}):
            raise ProxmoxError(f'Inventory visibility is restricted at {path}. Maintenance requires visibility of every host and guest.')


def prune(db, now):
    db.execute("DELETE FROM node_power_actions WHERE updated_at<? AND status IN ('completed','failed','acknowledged')", (now - telemetry.RETENTION,))
    db.execute('DELETE FROM node_maintenance WHERE enabled=0 AND updated_at<?', (now - telemetry.RETENTION,))


def memory_demand(configured, used, resident, mode, peaks=()):
    if mode == 'observed' and isinstance(used, (int, float)) and used > 0:
        demand = max(used, *peaks) if peaks else used
        return max(min(configured, demand * 1.2), resident or configured) + 256 * 1024**2
    return max(configured, resident or 0)


def capacity_budget(total, used, cores, cpu, ram, guest_cpu, pending_ram=0, pending_cpu=0, memory_mode='configured'):
    reserve = 2 * 1024**3 if memory_mode == 'observed' else max(total * .1, 2 * 1024**3)
    available_ram = max(0, total - used - reserve - pending_ram)
    available_cpu = max(0, (.8 - cpu) * cores - pending_cpu)
    return {'memory_required': ram, 'memory_available': available_ram, 'memory_shortfall': max(0, ram - available_ram),
            'host_memory_reserve': reserve, 'pending_memory': pending_ram, 'cpu_required': guest_cpu,
            'cpu_available': available_cpu, 'cpu_shortfall': max(0, guest_cpu - available_cpu), 'pending_cpu_cores': pending_cpu}


def capacity_reason(budget):
    gib = 1024**3
    if budget['memory_shortfall'] > 0:
        return (f'Target RAM headroom: guest requires {budget["memory_required"] / gib:.2f} GiB; '
                f'{budget["memory_available"] / gib:.2f} GiB available after host reserve and pending migrations; '
                f'short by {budget["memory_shortfall"] / gib:.2f} GiB '
                f'(host reserve {budget["host_memory_reserve"] / gib:.2f} GiB, pending {budget["pending_memory"] / gib:.2f} GiB).')
    if budget['cpu_shortfall'] > 1e-9:
        return (f'Target CPU headroom: guest requires {budget["cpu_required"]:.2f} cores; '
                f'{budget["cpu_available"]:.2f} cores available below the 80% admission limit; '
                f'short by {budget["cpu_shortfall"]:.2f} cores.')
    return None


def storage_retryable(check, node):
    return not check or check.get('node') != node or check.get('state') == 'unknown'


async def evacuation_plan(client, db, connection_id, node, now, restart_containers=True, reservations=()):
    from . import balancing, guest_policy, infrastructure, migration_groups, placement_rules
    await verify_inventory_access(client)
    resources, native_ha, ha_status = await asyncio.gather(client.get('cluster/resources'), client.get('cluster/ha/resources'), client.get('cluster/ha/status/current'))
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    if nodes.get(node, {}).get('status') != 'online':
        raise ProxmoxError('Source host must be online to verify and evacuate its guests.')
    remaining = residents(resources, node)
    if not remaining:
        return {'moves': [], 'remaining': [], 'blockers': []}
    excluded_nodes = excluded(db, connection_id) | {r['node'] for r in ha_status if r.get('node') and native_maintenance(r)}
    placement_policy = placement_rules.Policy(db, connection_id, resources, excluded_nodes)
    placements = {**placement_policy.placements, **{r['resource']: r['target'] for r in reservations if r.get('status') != 'failed'}}
    targets = {n: r for n, r in nodes.items() if n != node and n not in excluded_nodes and r.get('status') == 'online'}
    groups = migration_groups.list_groups(db, connection_id)
    infrastructure_status = infrastructure.latest(db, connection_id, now)
    checks = storage.checks(db, connection_id, now)
    samples = {s['resource']: s for s in telemetry.latest(db, connection_id, now)}
    config = balancing.settings(db, connection_id)
    memory_mode, target_limit = config['maintenance_memory_mode'], config['migration_target_concurrency']
    speed = balancing.performance(db, connection_id)
    owned = {r.get('sid') for r in native_ha}
    moves, blockers = [], []
    attempted = {r['resource']: r for r in reservations}
    for guest in remaining:
        resource = guest['id']
        if resource in attempted:
            previous = attempted[resource]
            if previous.get('status') == 'failed':
                blockers.append({'resource': resource, 'name': guest.get('name'), 'retryable': False, 'code': 'migration_failed',
                                 'reason': 'Migration failed in this evacuation; skipped while other guests move. ' + previous.get('message', '')})
            continue
        check = checks.get(resource)
        reason = storage.block_reason(check, node)
        retryable, code = storage_retryable(check, node), 'storage'
        if f'{"vm" if guest["type"] == "qemu" else "ct"}:{guest["vmid"]}' in owned:
            reason = 'Native HA owns this guest; evacuate it with Proxmox HA maintenance.'
            retryable, code = False, 'native_ha'
        elif guest.get('lock'):
            reason = 'Guest is locked.'
            retryable, code = True, 'guest_locked'
        elif guest.get('status') not in ('running', 'stopped'):
            reason = 'Guest running state is unknown.'
            retryable, code = True, 'guest_state'
        elif guest['type'] == 'lxc' and guest['status'] == 'running' and not restart_containers:
            reason = 'Container evacuation is disabled for this run. Enable Include containers (restart migration) and resume evacuation; this causes downtime.'
            retryable, code = False, 'container_restart'
        if reason:
            blockers.append({'resource': resource, 'name': guest.get('name'), 'reason': reason, 'retryable': retryable, 'code': code})
            continue
        running = guest['status'] == 'running'
        sample = samples.get(resource)
        if running and (not sample or not balancing.fresh(sample, now) or not sample['mem_total'] or not sample['cpu_count'] or sample['cpu'] is None):
            blockers.append({'resource': resource, 'name': guest.get('name'), 'reason': 'Waiting for fresh guest CPU and RAM measurements.', 'retryable': True, 'code': 'guest_metrics'})
            continue
        history = [telemetry.sample_dict(r) for r in db.execute('SELECT * FROM metric_samples WHERE connection_id=? AND resource=? AND sampled_at BETWEEN ? AND ?', (connection_id, resource, now - 300, now))]
        cpu = sample['cpu'] * sample['cpu_count'] if running else 0
        memory_peaks = [r['metadata'].get('summary', {}).get('max', {}).get('mem_used') or r['mem_used'] or 0 for r in history]
        guest_memory_mode = memory_mode if running and sample['mem_used'] is not None and sample['mem_used'] > 0 else 'configured'
        ram = memory_demand(sample['mem_total'], sample['mem_used'], sample['mem_host'], guest_memory_mode, memory_peaks) if running else 0
        options, failures = [], []
        for target in sorted(targets):
            target_cpu = cpu * speed.get(node, 1) / speed.get(target, 1)
            boundary = migration_groups.pair_policy(groups, nodes, node, target)
            failure = None if boundary['allowed'] else boundary['reason']
            retryable, code, budget = False, 'group_policy', None
            if not failure and infrastructure_status.get(target, {}).get('block_reason'):
                failure = infrastructure_status[target]['block_reason']
                retryable, code = True, 'infrastructure'
            if not failure:
                failure = storage.block_reason(check, node, target)
                retryable, code = storage_retryable(check, node), 'storage'
            host = samples.get(f'node/{target}')
            arriving = sum(r['target'] == target and r.get('settled_at') is None and r.get('status') != 'failed' for r in reservations)
            if not failure and target_limit and arriving >= target_limit:
                failure = f'{arriving} migrations are already arriving; the per-host limit is {target_limit}.'
                retryable, code = True, 'target_concurrency'
            pending = [r for r in reservations if r['target'] == target and (r['verified_at'] is None or not host or host['sampled_at'] <= r['verified_at'])]
            pending_ram = sum(r['reserved_memory'] for r in pending)
            pending_cpu = sum(r['reserved_cpu_cores'] for r in pending)
            if not failure and (not host or not balancing.fresh(host, now) or not host['mem_total'] or not host['cpu_count'] or host['mem_used'] is None or host['cpu'] is None):
                failure = 'Target needs fresh capacity measurements.'
                retryable, code = True, 'target_metrics'
            if not failure and running:
                budget = capacity_budget(host['mem_total'], host['mem_used'], host['cpu_count'], host['cpu'], ram, target_cpu, pending_ram, pending_cpu, guest_memory_mode)
                failure = capacity_reason(budget)
                retryable, code = True, 'capacity'
            if not failure and running:
                failure = balancing.target_pressure(host, {'recent': []}, now)
                retryable, code = True, 'pressure'
            if failure:
                failures.append({'node': target, 'reason': failure, 'retryable': retryable, 'code': code, 'capacity': budget})
                continue
            physical = max(ram, sample['mem_host'] or 0) if running else 0
            load = max((host['mem_used'] + pending_ram + physical) / host['mem_total'], host['cpu'] + (target_cpu + pending_cpu) / host['cpu_count'])
            allowed, rule_reason, overrides = placement_policy.check({resource: target}, placements, maintenance=True)
            if not allowed:
                failures.append({'node': target, 'reason': rule_reason, 'retryable': False, 'code': 'placement_rule', 'capacity': budget})
                continue
            rule_cost = placement_policy.violations({**placements, resource: target})
            options.append({'placement_cost': rule_cost, 'placement_overrides': overrides, 'resource': resource, 'name': guest.get('name') or resource, 'source': node, 'target': target,
                            'score': round(100 * (1 - load) + placement_policy.preference_gain({resource: target}, placements), 2), 'reserved_memory': ram, 'reserved_cpu_cores': target_cpu,
                            'pending_memory': pending_ram, 'pending_cpu_cores': pending_cpu,
                            'live_pending_memory': sum(r['reserved_memory'] for r in pending if r['verified_at'] is None),
                            'live_pending_cpu_cores': sum(r['reserved_cpu_cores'] for r in pending if r['verified_at'] is None),
                            'memory_mode': guest_memory_mode, 'configured_memory': sample['mem_total'] if running else 0,
                            'resident_memory': sample['mem_host'] if running else None,
                            'policy_allowed': True, 'urgency': 'maintenance', 'evaluation_track': 'maintenance',
                            'expected_status': guest['status'], 'kind': guest['type'], 'stickiness': guest_policy.read(db, connection_id, resource)['stickiness']})
        if options:
            moves.append(min(options, key=lambda move: (move['placement_cost'], -move['score'], move['target'])))
        else:
            blockers.append({'resource': resource, 'name': guest.get('name'), 'reason': '; '.join(f'{f["node"]}: {f["reason"]}' for f in failures) or 'No available target outside maintenance in the same enabled migration domain.',
                             'retryable': any(f['retryable'] for f in failures), 'targets': failures})
    moves.sort(key=lambda move: (move['expected_status'] != 'running', -move['score'], move['resource']))
    return {'moves': moves, 'remaining': [{'resource': g['id'], 'name': g.get('name'), 'status': g['status']} for g in remaining], 'blockers': blockers}


async def power_preflight(client, db, connection_id, node):
    from . import migration_queue
    state = read(db, connection_id, node)
    if not state['enabled'] or state['state'] != 'maintenance':
        raise ProxmoxError('Enter maintenance and finish evacuation before powering this host.')
    if migration_queue.active(db, connection_id):
        raise ProxmoxError('Stop or finish the migration queue before a host power action.')
    migration_queue.ensure_idle(db, connection_id)
    await verify_inventory_access(client)
    resources, quorum, tasks, ha = await asyncio.gather(client.get('cluster/resources'), client.get('cluster/status'), client.get('cluster/tasks'), client.get('cluster/ha/resources'))
    if not isinstance(ha, list):
        raise ProxmoxError('Native HA resource inventory is unavailable.')
    if residents(resources, node):
        raise ProxmoxError('Host is not empty. Evacuate all VMs and containers, including stopped guests, first.')
    nodes = [r for r in resources if r.get('type') == 'node']
    if not any(r['node'] == node and r.get('status') == 'online' for r in nodes):
        raise ProxmoxError('Host must be online before submitting a power action.')
    if not any(r.get('type') == 'cluster' and r.get('quorate') in (1, True) for r in quorum):
        raise ProxmoxError('Cluster quorum could not be confirmed.')
    if sum(r.get('status') == 'online' and r['node'] != node for r in nodes) < len(nodes) // 2 + 1:
        raise ProxmoxError('Power action would leave fewer than a majority of cluster nodes online. Review quorum in Proxmox.')
    if ha:
        native = await native_ha.inspect(client)
        if not native_ha.observe(db, connection_id, node, native, resources, time.time()).get('ready'):
            raise ProxmoxError('Native HA maintenance must be confirmed and fully evacuated before a host power operation.')
    if any(not t.get('endtime') and t.get('status') in (None, 'running', 'unknown') and (t.get('node') == node or t.get('type') in ('qmigrate', 'vzmigrate')) for t in tasks):
        raise ProxmoxError('A host task or migration is active or unresolved.')
    status = await client.get(f'nodes/{segment(node)}/status')
    if not isinstance(status.get('uptime'), (int, float)):
        raise ProxmoxError('Host uptime is unavailable; cannot track the power action.')
    return status['uptime']


async def refresh(client, db, connection_id, node, now):
    result = read(db, connection_id, node)
    await verify_inventory_access(client)
    resources = await client.get('cluster/resources')
    if result['native_ha']:
        native = await native_ha.inspect(client)
        native_ha.observe(db, connection_id, node, native, resources, now)
        result = read(db, connection_id, node)
    host = next((r for r in resources if r.get('type') == 'node' and r.get('node') == node), None)
    if not host:
        raise ProxmoxError('Host is not visible in this cluster.')
    remaining = residents(resources, node)
    result['online'] = host.get('status') == 'online'
    result['remaining'] = [{'resource': r['id'], 'name': r.get('name'), 'status': r.get('status')} for r in remaining]
    if result['enabled'] and result['state'] == 'maintenance' and remaining:
        result.update(state='blocked', message='Guests are present again. Resume evacuation before host power actions.')
    power = result['power_action']
    if power and power['status'] in ('accepted', 'unknown'):
        completed = power['command'] == 'shutdown' and not result['online']
        if power['command'] == 'reboot' and result['online']:
            detail = await client.get(f'nodes/{segment(node)}/status')
            uptime = detail.get('uptime')
            completed = isinstance(uptime, (int, float)) and power['uptime'] is not None and uptime < power['uptime'] + now - power['created_at'] - 2
        status = 'completed' if completed else 'unknown' if now - power['created_at'] > 300 else power['status']
        if status != power['status']:
            message = 'Host power transition verified.' if completed else 'Power transition could not be verified. Check the host before acknowledging or retrying.'
            db.execute('UPDATE node_power_actions SET status=?,message=?,updated_at=? WHERE id=?', (status, message, now, power['id']))
            result['power_action'] = {**power, 'status': status, 'message': message}
    if result['enabled'] and result['state'] == 'blocked' and remaining:
        set_state(db, connection_id, node, result['state'], result['message'], now, result['blockers'])
    return result
