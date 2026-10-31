import asyncio

from fastapi import HTTPException

from . import infrastructure, balancing, migration_groups, migration_queue, node_maintenance, placement_rules, storage, telemetry
from .operations import action_request
from .proxmox import ProxmoxError, segment

# Starting where the guest already is avoids a migration unless another host is clearly less loaded.
MARGIN = .1


def recommend(db, connection_id, snapshot, resource, now, hosts=None):
    guest = next((g for g in snapshot.get('guests', []) if g.get('id') == resource), None)
    if not guest:
        raise HTTPException(404, 'Guest not found in the current inventory.')
    if guest.get('status') != 'stopped':
        raise HTTPException(409, 'Only stopped guests can be started on the best host.')
    source = guest['node']
    if hosts is None:
        hosts = {s['node']: host_metrics({'cpu': s['cpu'], 'cpuinfo': {'cpus': s['cpu_count']},
                                        'memory': {'total': s['mem_total'], 'used': s['mem_used']}})
                 for s in telemetry.latest(db, connection_id, now)
                 if s['resource'].startswith('node/') and balancing.fresh(s, now)}
    nodes = {n['node']: n for n in snapshot.get('nodes', [])}
    groups = migration_groups.list_groups(db, connection_id)
    domain = {n for g in groups if g['enabled'] and source in g['nodes'] for n in g['nodes']} | {source}
    excluded = node_maintenance.excluded(db, connection_id)
    check = storage.checks(db, connection_id, now).get(resource)
    # Rules apply as if the guest were already running on its current host.
    resources = snapshot.get('nodes', []) + [{**g, 'status': 'running'} if g.get('id') == resource else g for g in snapshot.get('guests', [])]
    policy = placement_rules.Policy(db, connection_id, resources)
    speed = balancing.performance(db, connection_id)
    cores = guest.get('maxcpu') or 1
    cpu = .25 * cores
    ram = guest.get('maxmem') or 0
    infra = infrastructure.latest(db, connection_id, now)
    options = []
    for node in sorted(domain):
        host, reason = hosts.get(node), None
        if nodes.get(node, {}).get('status') != 'online' or node in excluded:
            reason = 'Host is offline or in maintenance.'
        elif not host:
            reason = 'Host needs fresh CPU/RAM measurements.'
        elif node != source and not check:
            reason = 'Guest storage has not been verified for migration.'
        elif node != source:
            reason = storage.block_reason(check, source, node)
        if not reason:
            reason = infra.get(node, {}).get('block_reason')
        if not reason:
            allowed, rule_reason, _ = policy.check({resource: node})
            reason = None if allowed else rule_reason
        load = None
        if not reason:
            memory = host['memory'] + ram / host['ram']
            processor = host['cpu'] + cpu * speed.get(source, 1) / speed.get(node, 1) / host['cores']
            load = round(max(memory, processor), 3)
            if memory > .9 or processor > .8:
                reason = f'Projected load {memory:.0%} RAM / {processor:.0%} CPU exceeds the start limits of 90% RAM and 80% CPU.'
        options.append({'node': node, 'load': load, 'rank': load - policy.preference_gain({resource: node}) / 100 if load is not None else None, 'reason': reason})
    usable = [o for o in options if not o['reason']]
    current = next((o for o in usable if o['node'] == source), None)
    best = min(usable, key=lambda o: (o['rank'], o['node'] != source, o['node']), default=None)
    choice = best if current is None or best['rank'] < current['rank'] - MARGIN else current
    return {'resource': resource, 'current': source, 'target': choice['node'] if choice else None,
            'migrate': bool(choice and choice['node'] != source), 'cpu_cores': round(cpu, 2), 'memory': ram, 'options': options}


def host_metrics(status):
    cpu, cores = status.get('cpu'), status.get('cpuinfo', {}).get('cpus')
    ram, used = status.get('memory', {}).get('total'), status.get('memory', {}).get('used')
    if any(not isinstance(v, (int, float)) or v < 0 for v in (cpu, cores, ram, used)) or not cores or not ram:
        return None
    return {'cpu': cpu, 'cores': cores, 'ram': ram, 'memory': used / ram}


async def live_recommend(client, database, connection_id, resource, now):
    resources = await client.get('cluster/resources')
    guest = next((g for g in resources if g.get('id') == resource), None)
    if not guest or guest.get('status') != 'stopped':
        raise ProxmoxError('Start on best host requires a visible, stopped guest.')
    current = await client.get(f'nodes/{segment(guest["node"])}/{resource}/status/current')
    guest = {**guest, 'status': current.get('status'), 'maxmem': current.get('maxmem'), 'maxcpu': current.get('cpus')}
    if not guest['maxmem'] or not guest['maxcpu']:
        raise ProxmoxError('Guest RAM and CPU configuration could not be verified.')
    nodes = [n for n in resources if n.get('type') == 'node']
    online = [n['node'] for n in nodes if n.get('status') == 'online']
    readings = await asyncio.gather(*(client.get(f'nodes/{segment(n)}/status') for n in online), return_exceptions=True)
    hosts = {n: host_metrics(s) for n, s in zip(online, readings) if isinstance(s, dict)}
    snapshot = {'nodes': nodes, 'guests': [guest if g.get('id') == resource else g for g in resources if g.get('type') in ('qemu', 'lxc')]}
    with database() as db:
        return recommend(db, connection_id, snapshot, resource, now, hosts)


def settle_migration(db, connection_id, task, resource, plan, now):
    """Record the verified offline migration as finished so the start step does not wait for the activity poll."""
    fields = task['upid'].split(':')
    try:
        started = int(fields[4], 16)
    except (IndexError, ValueError):
        started = now
    kind = 'vzmigrate' if resource.startswith('lxc/') else 'qmigrate'
    # A later stale "running" observation cannot downgrade a completed row.
    db.execute('''INSERT INTO native_tasks(connection_id,upid,node,resource,kind,started_at,finished_at,status,message,source,target,origin,reason,observed_at)
        VALUES(?,?,?,?,?,?,?,'completed','OK',?,?,'ProxHA','Manual migrate in ProxHA.',?)
        ON CONFLICT(connection_id,upid) DO UPDATE SET status='completed',message='OK',
        finished_at=COALESCE(native_tasks.finished_at,excluded.finished_at),target=COALESCE(native_tasks.target,excluded.target)''',
        (connection_id, task['upid'], fields[1], resource, kind, started, now, plan['current'], plan['target'], now))


async def prepare(client, database, connection_id, operation, plan):
    resource, target = plan['resource'], plan['target']
    resources, quorum, ha_status, ha = await asyncio.gather(*(client.get(path) for path in
        ('cluster/resources', 'cluster/status', 'cluster/ha/status/current', 'cluster/ha/resources')))
    if not any(r.get('type') == 'cluster' and r.get('quorate') in (1, True) for r in quorum):
        raise ProxmoxError('Cluster quorum could not be confirmed.')
    guest = next((g for g in resources if g.get('id') == resource), None)
    if not guest or guest.get('type') != resource.split('/')[0]:
        raise ProxmoxError('Guest is no longer visible in this cluster.')
    if any(r.get('sid') == placement_rules.ha_sid(resource) for r in ha):
        raise ProxmoxError('Native HA owns this guest; use Proxmox HA to start it.')
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    if nodes.get(target, {}).get('status') != 'online' or any(r.get('node') == target and node_maintenance.native_maintenance(r) for r in ha_status):
        raise ProxmoxError('The start target is offline or in maintenance.')
    source = plan['current'] if operation.action == 'migrate' else target
    base = f'nodes/{segment(source)}/{resource}'
    current, config, status = await asyncio.gather(client.get(base + '/status/current'),
        client.get(base + '/config?current=1'), client.get(f'nodes/{segment(target)}/status'))
    if current.get('status') != 'stopped' or current.get('qmpstatus', 'stopped') != 'stopped' or config.get('lock'):
        raise ProxmoxError('The guest must be confirmed stopped and unlocked on the expected host.')
    ram, cores = current.get('maxmem'), current.get('cpus')
    host = host_metrics(status)
    if not host or not isinstance(ram, (int, float)) or ram <= 0 or not isinstance(cores, (int, float)) or cores <= 0:
        raise ProxmoxError('Live host capacity or guest configuration is incomplete.')
    with database() as db:
        if migration_queue.active(db, connection_id):
            raise ProxmoxError('Wait for the migration queue before starting on the best host.')
        migration_queue.ensure_idle(db, connection_id)
        if target in node_maintenance.excluded(db, connection_id):
            raise ProxmoxError('The start target entered maintenance.')
        reason = infrastructure.latest(db, connection_id, telemetry.time.time()).get(target, {}).get('block_reason')
        if reason:
            raise ProxmoxError(reason)
        speed = balancing.performance(db, connection_id)
        if operation.action == 'migrate':
            boundary = migration_groups.pair_policy(migration_groups.list_groups(db, connection_id), nodes, source, target)
            if not boundary['allowed']:
                raise ProxmoxError(boundary['reason'])
        await placement_rules.live_check(client, db, connection_id, {resource: target}, starting=True)
    cpu = max(plan['cpu_cores'], .25 * cores) * speed.get(plan['current'], 1) / speed.get(target, 1)
    if host['memory'] + ram / host['ram'] > .9 or host['cpu'] + cpu / host['cores'] > .8:
        raise ProxmoxError('Live target capacity exceeds the start limits of 90% RAM or 80% CPU.')
    if operation.action == 'migrate':
        move = {'resource': resource, 'source': source, 'target': target, 'expected_status': 'stopped'}
        await migration_queue.live_preflight(client, move, maintenance=True)
    guest = {**guest, 'node': source, 'template': config.get('template', guest.get('template'))}
    path, params = action_request(operation, guest, current)
    return [(guest, path, params)]
