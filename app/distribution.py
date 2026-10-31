from collections import Counter

GIB = 1024 ** 3
WEIGHTS = {'count': 3, 'classes': 3, 'cpu': 30, 'resident': 20, 'configured': 10}


def workload(guest, demand=None):
    cores, ram = guest['cpu_count'], guest['mem_total']
    cpu = demand['busy_cores'] if demand and demand.get('busy_cores') is not None else guest['cpu'] * cores
    resident = guest['mem_host']
    # Host-resident RAM of a long-running VM approaches its configured size, so load
    # uses the RAM the guest reports as used when balloon statistics are available.
    reported = (guest.get('metadata') or {}).get('memory_source') == 'guest balloon statistics' and guest.get('mem_used') is not None
    active = min(resident, guest['mem_used']) if reported else resident
    size = 'small' if ram <= 4 * GIB and cores <= 4 else 'medium' if ram <= 16 * GIB and cores <= 8 else 'large'
    utilization = max(cpu / cores, active / ram)
    load = 'low' if utilization < .25 else 'medium' if utilization < .65 else 'high'
    return {'size': size, 'load': load, 'category': f'{size}/{load}', 'cpu': cpu,
            'resident': resident, 'active_memory': active, 'configured': ram,
            'measurement_basis': 'five-minute mean CPU, guest-reported used RAM' if reported else 'five-minute mean CPU, resident RAM'}


def prepare(models, samples, groups, excluded, now, profiles=None):
    domains, workloads = [], {}
    for group in groups:
        members = sorted(n for n in group['nodes'] if n in models and n not in excluded)
        if not group['enabled'] or len(members) < 2:
            continue
        guests = [g for g in samples.values() if g['resource'].startswith(('qemu/', 'lxc/')) and g['status'] == 'running' and g['node'] in members]
        if any(not 0 <= now - g['observed_at'] <= 45 or any(g[k] is None for k in ('mem_host', 'cpu')) or not g['mem_total'] or not g['cpu_count'] for g in guests):
            domains.append({'id': group['id'], 'name': group['name'], 'nodes': members, 'available': False,
                            'reason': 'Fresh CPU, resident RAM and configuration are required for every running guest in the group.'})
            continue
        # CPU targets use reference cores: logical cores weighted by relative host performance.
        speed = {n: models[n].get('performance', 1) for n in members}
        for guest in guests:
            workloads[guest['resource']] = item = workload(guest, (profiles or {}).get(guest['resource']))
            item['cpu'] *= speed[guest['node']]
        totals = {k: sum(workloads[g['resource']][k] for g in guests) for k in ('cpu', 'resident', 'configured')}
        classes = dict(Counter(workloads[g['resource']]['category'] for g in guests))
        cores = sum(models[n]['cores'] * speed[n] for n in members)
        ram = sum(models[n]['ram'] for n in members)
        for node in members:
            host = models[node]
            cpu_share, ram_share = host['cores'] * speed[node] / cores, host['ram'] / ram
            share = (cpu_share + ram_share) / 2
            resident_guests = [g for g in guests if g['node'] == node]
            current = {'count': len(resident_guests), 'classes': dict(Counter(workloads[g['resource']]['category'] for g in resident_guests)),
                       **{k: sum(workloads[g['resource']][k] for g in resident_guests) for k in totals}}
            target = {'count': len(guests) * share, 'classes': {c: total * share for c, total in classes.items()},
                      'cpu': totals['cpu'] * cpu_share, 'resident': totals['resident'] * ram_share, 'configured': totals['configured'] * ram_share}
            host['distribution'] = {'group_id': group['id'], 'current': current, 'target': target,
                                    'cpu_share': cpu_share, 'ram_share': ram_share,
                                    'cpu_scale': max(totals['cpu'] / cores, .05) * host['cores'] * speed[node],
                                    'resident_scale': max(totals['resident'] / ram, .1) * host['ram'],
                                    'configured_scale': max(totals['configured'] / ram, .5) * host['ram']}
        domains.append({'id': group['id'], 'name': group['name'], 'nodes': members, 'available': True,
                        'running_guests': len(guests), 'running_vms': sum(g['resource'].startswith('qemu/') for g in guests),
                        'running_containers': sum(g['resource'].startswith('lxc/') for g in guests), 'classes': classes})
    return domains, workloads


def components(host):
    data = host['distribution']
    current, target = data['current'], data['target']
    return {'count': (current['count'] - target['count']) ** 2 / max(1, target['count']),
            'classes': sum((current['classes'].get(c, 0) - value) ** 2 / max(1, value) for c, value in target['classes'].items()),
            **{key: data['cpu_share' if key == 'cpu' else 'ram_share'] * ((current[key] - target[key]) / data[f'{key}_scale']) ** 2 for key in ('cpu', 'resident', 'configured')}}


def cost(host):
    return sum(WEIGHTS[k] * v for k, v in components(host).items())


def transfer(host, workload, direction):
    data = host['distribution']
    current = data['current']
    classes = {**current['classes'], workload['category']: current['classes'].get(workload['category'], 0) + direction}
    changed = {'count': current['count'] + direction, 'classes': classes,
               **{k: max(0, current[k] + direction * workload[k]) for k in ('cpu', 'resident', 'configured')}}
    return {**host, 'distribution': {**data, 'current': changed}}


def improvement(origin, target, workload):
    first, second = origin.get('distribution'), target.get('distribution')
    if not workload or not first or not second or first['group_id'] != second['group_id']:
        return None
    after_source, after_target = transfer(origin, workload, -1), transfer(target, workload, 1)
    before, destination = components(origin), components(target)
    after, moved = components(after_source), components(after_target)
    parts = {key: WEIGHTS[key] * (before[key] + destination[key] - after[key] - moved[key]) for key in WEIGHTS}
    raw = sum(parts.values())
    scale = min(1, 50 / abs(raw)) if raw else 1
    parts = {key: value * scale for key, value in parts.items()}
    return {'gain': sum(parts.values()), 'raw_gain': raw, 'parts': parts, 'source': after_source['distribution'], 'target': after_target['distribution'], 'workload': workload}


def reasons(host, effort):
    data = host.get('distribution')
    if not data:
        return []
    current, target = data['current'], data['target']
    tolerance = {'conservative': 2, 'balanced': 1, 'thorough': .5}[effort]
    result = []
    if current['count'] - target['count'] > tolerance:
        result.append(f'Guest count exceeds the group target: {current["count"]} running / {target["count"]:.1f} target.')
    over = [c for c, value in current['classes'].items() if value - target['classes'].get(c, 0) > max(.75, tolerance)]
    if over:
        result.append('Workload categories are concentrated on this host: ' + ', '.join(over) + '.')
    for key, label in (('cpu', 'five-minute mean CPU demand'), ('resident', 'current resident RAM')):
        if current[key] - target[key] > max(target[key] * .15, data[f'{key}_scale'] * .1):
            result.append(f'Above the group target for {label}.')
    return result


def describe(domains, models, projected, moves):
    result = []
    for domain in domains:
        if not domain['available']:
            result.append(domain)
            continue
        nodes = [{'node': n, 'current': models[n]['distribution']['current'], 'target': models[n]['distribution']['target'],
                   'projected': projected[n]['distribution']['current']} for n in domain['nodes']]
        result.append({**domain, 'hosts': nodes, 'cost_before': round(sum(cost(models[n]) for n in domain['nodes']), 2),
                       'cost_after': round(sum(cost(projected[n]) for n in domain['nodes']), 2)})
    return {'groups': result, 'weights': WEIGHTS, 'conditional_moves': len(moves), 'horizon_limit': 32,
            'description': 'Capacity-weighted VM and container counts, workload categories, five-minute mean CPU and current RAM demand. Targets include fixed guests. VMs migrate live; running containers use restart migration with downtime. Fractional targets are goals, not reservations.'}
