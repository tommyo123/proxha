import math

from . import telemetry

MIB = 1024 ** 2
CONFIG_TTL = 60
MARGIN_PERCENT = 2
MIN_GAIN = 64 * MIB


def node_config(config, now):
    value = config.get('ballooning-target', 80)
    try:
        target = float(value)
    except (TypeError, ValueError):
        target = 0
    if not math.isfinite(target) or not 0 < target <= 100:
        return {'target_percent': None, 'checked_at': now, 'error': 'Invalid ballooning target.'}
    return {'target_percent': target, 'checked_at': now,
            'source': 'configured' if 'ballooning-target' in config else 'Proxmox default'}


def guest_reclaimable(guest, now):
    control = guest.get('metadata', {}).get('balloon_control', {})
    updated = guest.get('metadata', {}).get('balloon_stats', {}).get('last_update')
    minimum, maximum = control.get('minimum'), guest.get('mem_total')
    if (not guest['resource'].startswith('qemu/') or guest.get('status') != 'running'
            or not 0 <= now - guest['observed_at'] <= 45
            or not minimum or not maximum or minimum >= maximum or control.get('shares') == 0
            or not guest.get('balloon') or guest['balloon'] > maximum
            or updated is None or not 0 <= now - updated <= 60
            or guest.get('metadata', {}).get('inventory', {}).get('lock')):
        return 0
    return max(0, maximum - guest['balloon'])


def host_fields(sample, guests, now, enabled=True):
    config = sample['metadata'].get('balloon_config', {})
    checked = config.get('checked_at')
    target = config.get('target_percent') if checked is not None and 0 <= now - checked <= CONFIG_TTL + 45 else None
    deficits = [guest_reclaimable(g, now) for g in guests]
    return {'balloon_target_percent': target, 'balloon_target_source': config.get('source'),
            'balloon_config_error': config.get('error') or (None if target is not None else 'Ballooning target is unavailable or stale.'),
            'balloon_recoverable': sum(deficits), 'balloon_recovery_guests': sum(d > 0 for d in deficits),
            'balloon_recovery_enabled': enabled, 'balloon_margin_percent': MARGIN_PERCENT}


def available(host, used=None, margin=False):
    target = host.get('balloon_target_percent')
    if target is None:
        return None
    target = max(0, target - (MARGIN_PERCENT if margin else 0)) / 100
    used = host['memory'] * host['ram'] if used is None else used
    return max(0, target * host['ram'] - used)


def pending(host):
    room = available(host)
    debt = host.get('balloon_recoverable', 0)
    return max(0, debt - room) if room is not None else None


def reasons(host):
    debt = pending(host)
    if host.get('balloon_recovery_enabled') and debt is not None and debt > MIN_GAIN:
        return [f'Balloon recovery needs more capacity below the {host["balloon_target_percent"]:g}% host target.']
    return []


def settle(host, used, debt, reclaimed):
    room = available(host, used)
    refill = min(debt, room) if room is not None else 0
    return {**host, 'memory': (used + refill) / host['ram'],
            'memory_recent': (used + refill) / host['ram'],
            'balloon_recoverable': max(0, debt - refill),
            'balloon_reclaimed': max(0, reclaimed - refill) if reclaimed is not None else None}, refill


def project(origin, destination, guest, resident, now):
    baseline_source, _ = settle(origin, origin['memory'] * origin['ram'], origin.get('balloon_recoverable', 0), origin.get('balloon_reclaimed'))
    baseline_target, _ = settle(destination, destination['memory'] * destination['ram'], destination.get('balloon_recoverable', 0), destination.get('balloon_reclaimed'))
    reclaimed = guest_reclaimable(guest, now)
    source_debt = max(0, origin.get('balloon_recoverable', 0) - reclaimed)
    target_debt = destination.get('balloon_recoverable', 0) + reclaimed
    source_raw = max(0, origin['memory'] * origin['ram'] - guest['mem_host'])
    target_raw = destination['memory'] * destination['ram'] + resident
    reported = guest.get('balloon_reclaimed') or 0
    source_reported = max(0, origin['balloon_reclaimed'] - reported) if origin.get('balloon_reclaimed') is not None else None
    target_reported = destination['balloon_reclaimed'] + reported if destination.get('balloon_reclaimed') is not None else None
    source, refill = settle(origin, source_raw, source_debt, source_reported)
    target, target_refill = settle(destination, target_raw, target_debt, target_reported)
    debts = [pending(h) for h in (origin, destination, source, target)]
    gain = debts[0] + debts[1] - debts[2] - debts[3] if all(d is not None for d in debts) else 0
    enabled = origin.get('balloon_recovery_enabled', False)
    score = 40 * gain / min(origin['ram'], destination['ram']) if enabled else 0
    if enabled and gain > MIN_GAIN and reclaimed and target_debt <= (available(destination, target_raw) or 0):
        score += 4 * min(1, gain / reclaimed)
    return {'source': source, 'target': target, 'source_refill': refill, 'target_refill': target_refill,
            'baseline_source': baseline_source, 'baseline_target': baseline_target,
            'gain_bytes': gain, 'score': max(-40, min(40, score)), 'incoming_reclaimed': reclaimed}


def describe(hosts):
    rows = []
    for host in hosts:
        room = available(host) if host.get('ram') else None
        debt = host.get('balloon_recoverable', 0)
        eligible = min(debt, room) if room is not None else None
        rows.append({'node': host['node'], 'target_percent': host.get('balloon_target_percent'),
                     'target_source': host.get('balloon_target_source'), 'error': host.get('balloon_config_error'),
                     'ballooned_bytes': host.get('balloon_reclaimed'), 'recoverable_bytes': debt,
                     'eligible_bytes': eligible, 'blocked_bytes': debt - eligible if eligible is not None else None,
                     'guests': host.get('balloon_recovery_guests', 0),
                     'unmanaged_bytes': max(0, (host.get('balloon_reclaimed') or 0) - debt),
                     'state': 'unknown' if room is None else 'needs_capacity' if debt - eligible > MIN_GAIN else 'native_recovery' if debt else 'unverified' if (host.get('balloon_reclaimed') or 0) > MIN_GAIN else 'restored'})
    return {'controller': 'Proxmox native ballooning', 'planning_margin_percent': MARGIN_PERCENT,
            'recoverable_bytes': sum(r['recoverable_bytes'] for r in rows),
            'eligible_bytes': sum(r['eligible_bytes'] or 0 for r in rows),
            'blocked_bytes': sum(r['blocked_bytes'] or 0 for r in rows), 'hosts': rows}


def progress(db, connection_id, samples, now, recovery):
    rows = db.execute("SELECT * FROM metric_observations WHERE connection_id=? AND resource LIKE 'qemu/%' AND sampled_at>=? AND sampled_at<=? ORDER BY sampled_at",
                      (connection_id, now - 330, now - 300)).fetchall()
    before = {row['resource']: telemetry.sample_dict(row) for row in rows}
    returned, covered = {}, {}
    for resource, old in before.items():
        current = samples.get(resource)
        if (not current or current['node'] != old['node'] or current['mem_total'] != old['mem_total']
                or current['status'] != 'running' or old['status'] != 'running'
                or current.get('balloon') is None or old.get('balloon') is None
                or not 0 <= now - current['observed_at'] <= 45):
            continue
        if any(s['metadata'].get('balloon_stats', {}).get('last_update') is None or
               not 0 <= s['observed_at'] - s['metadata']['balloon_stats']['last_update'] <= 60 for s in (old, current)):
            continue
        node = current['node']
        returned[node] = returned.get(node, 0) + current['balloon'] - old['balloon']
        covered[node] = covered.get(node, 0) + 1
    for host in recovery['hosts']:
        host['returned_bytes_5m'] = returned.get(host['node'])
        host['progress_guests'] = covered.get(host['node'], 0)
        if host['state'] == 'native_recovery' and host['returned_bytes_5m'] is not None and host['returned_bytes_5m'] <= MIN_GAIN and host['recoverable_bytes'] > max(MIN_GAIN, host['guests'] * 100 * MIB):
            host['state'] = 'native_wait'
    recovery['returned_bytes_5m'] = sum(returned.values()) if returned else None
    recovery['progress_note'] = 'Net change in balloon allocation over five minutes for observed running VMs with unchanged host and configured RAM. Positive means RAM returned; negative means RAM reclaimed.'
