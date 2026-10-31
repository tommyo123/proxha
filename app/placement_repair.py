from . import balloon, distribution

CPU_LIMIT = .9


def host_models(samples, now):
    from . import balancing
    models = {}
    for resource, sample in samples.items():
        if not resource.startswith('node/') or sample['status'] != 'online' or not balancing.fresh(sample, now):
            continue
        if any(sample.get(k) is None for k in ('cpu', 'cpu_count', 'mem_used', 'mem_total')) or not sample['cpu_count'] or not sample['mem_total']:
            continue
        demand = {'cpu': sample['cpu'], 'memory': sample['mem_used'] / sample['mem_total'],
                  'peak_cores': sample['cpu'] * sample['cpu_count']}
        guests = [s for r, s in samples.items() if r.startswith('qemu/') and s['node'] == sample['node'] and s['status'] == 'running']
        models[sample['node']] = {**balancing.model(sample, demand),
            **balancing.memory_commitments(samples, sample['node'], now),
            **balloon.host_fields(sample, guests, now, False), 'repair_only': True}
    return models


def capacity_reason(total, used, ram, pending=0, cores=None, cpu=None, guest_cpu=0, pending_cpu=0):
    reserve = 2 * 1024**3
    available = max(0, total - used - pending - reserve)
    if ram > available:
        return (f'Target cannot fit rule-repair RAM: {ram / 1024**3:.2f} GiB required; '
                f'{available / 1024**3:.2f} GiB available after pending migrations and a 2 GiB host reserve.')
    if cores:
        # Repair may exceed the routine 80% admission limit, but never saturate the target.
        cpu_available = max(0, (CPU_LIMIT - cpu) * cores - pending_cpu)
        if guest_cpu - cpu_available > 1e-9:
            return (f'Target cannot fit rule-repair CPU: {guest_cpu:.2f} cores required; '
                    f'{cpu_available:.2f} cores available below the {CPU_LIMIT:.0%} repair limit after pending migrations.')
    return None


def member_move(guest, source, target, models, policy, workload=None):
    origin, destination = models[source], models[target]
    ram = max(guest['mem_total'], guest['mem_host'])
    cpu = guest['cpu'] * guest['cpu_count']
    arriving = cpu * origin.get('performance', 1) / destination.get('performance', 1)
    reason = capacity_reason(destination['ram'], destination['memory'] * destination['ram'], ram,
                             cores=destination['cores'], cpu=destination['cpu_live'], guest_cpu=arriving)
    if reason:
        return None, reason
    resident = guest['mem_host']
    after_source = {**origin, 'cpu': max(0, origin['cpu'] - cpu / origin['cores']),
        'cpu_live': max(0, origin['cpu_live'] - cpu / origin['cores']),
        'cpu_recent': max(0, origin['cpu_recent'] - cpu / origin['cores']),
        'memory': max(0, origin['memory'] - resident / origin['ram']),
        'running_guests': max(0, origin.get('running_guests', 1) - 1),
        'assigned_memory': max(0, origin['assigned_memory'] - guest['mem_total']) if origin['assigned_memory'] is not None else None}
    after_target = {**destination, 'cpu': destination['cpu'] + arriving / destination['cores'],
        'cpu_live': destination['cpu_live'] + arriving / destination['cores'],
        'cpu_recent': destination['cpu_recent'] + arriving / destination['cores'],
        'memory': destination['memory'] + resident / destination['ram'],
        'running_guests': destination.get('running_guests', 0) + 1,
        'assigned_memory': destination['assigned_memory'] + guest['mem_total'] if destination['assigned_memory'] is not None else None}
    allocation = distribution.improvement(origin, destination, workload)
    if allocation:
        after_source['distribution'], after_target['distribution'] = allocation['source'], allocation['target']
    container = guest['resource'].startswith('lxc/')
    return {'resource': guest['resource'], 'name': guest['metadata'].get('name') or guest['resource'],
        'guest_type': 'lxc' if container else 'qemu', 'migration_mode': 'restart' if container else 'live',
        'requires_downtime': container, 'source': source, 'target': target, 'group_id': policy['group_id'],
        'stickiness': policy['stickiness'], 'policy_allowed': True, 'policy_reason': None,
        'urgency': 'routine', 'score': 100, 'score_unit': 'priority points', 'score_breakdown': {},
        'reserved_memory': ram, 'reserved_cpu_cores': arriving, 'expected_memory': resident, 'expected_cpu_cores': arriving,
        'rule_repair': True, 'selection_goal': 'placement_rules', 'requires_storage_review': False,
        'requires_preflight': True, 'execution_allowed': False, 'balloon_admission': None,
        'potential_balloon_recovery': 0,
        'projection': {'source_before': origin, 'target_before': destination,
                       'source_after': after_source, 'target_after': after_target}}, None
