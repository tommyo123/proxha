import math

from . import telemetry

DEFAULTS = {'cpu_threshold_percent': 80, 'memory_threshold_percent': 80,
            'overload_hold_seconds': 300, 'cpu_psi_threshold_percent': 5}


def evidence(db, connection_id, host, samples, now, config, previous=None):
    window = config['overload_hold_seconds']
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM metric_observations WHERE connection_id=? AND resource=? AND sampled_at>=? AND sampled_at<=? ORDER BY sampled_at",
        (connection_id, 'node/' + host['node'], now - window - 45, now))]
    rows = [r for r in rows if r['quality'] != 'unavailable' and r['source_at'] is not None and 0 <= r['observed_at'] - r['source_at'] <= 180]
    if rows:
        rows = [r for r in rows if r['observed_at'] >= rows[-1]['observed_at'] - window]
    covered = (len(rows) >= math.ceil(window / telemetry.INTERVAL * .8) and
               rows[-1]['observed_at'] - rows[0]['observed_at'] >= window - telemetry.INTERVAL and
               0 <= now - rows[-1]['observed_at'] <= 45 and
               all(b['observed_at'] - a['observed_at'] <= 45 for a, b in zip(rows, rows[1:])))
    prior = (previous or {}).get('relief') or {}
    cpu_limit = (config['cpu_threshold_percent'] - (5 if prior.get('cpu_ready') else 0)) / 100
    memory_limit = (config['memory_threshold_percent'] - (5 if prior.get('memory_ready') else 0)) / 100

    def sustained(values, limit):
        return bool(covered and all(v is not None for v in values) and values[-1] >= limit and
                    sum(v >= limit for v in values) / len(values) >= .8 and sum(values) / len(values) >= limit)

    cpu_high = sustained([r['cpu'] for r in rows], cpu_limit)
    memory_high = sustained([r['mem_used'] / r['mem_total'] if r['mem_total'] and r['mem_used'] is not None else None for r in rows], memory_limit)
    psi_high = sustained([r['pressurecpusome'] for r in rows], config['cpu_psi_threshold_percent'])
    guests = [s for s in samples.values() if s['node'] == host['node'] and s['resource'].startswith(('qemu/', 'lxc/'))
              and s['status'] == 'running' and s['cpu'] is not None and s['cpu_count'] and 0 <= now - s['observed_at'] <= 45]
    busy = sorted([(s['resource'], s['cpu'] * s['cpu_count'] / host['cpu_count']) for s in guests], key=lambda item: -item[1])
    heavy = [resource for resource, share in busy if share >= .10]
    return {'cpu_ready': cpu_high and (len(heavy) >= 2 or psi_high), 'memory_ready': memory_high,
            'cpu_high': cpu_high, 'cpu_psi_ready': psi_high, 'heavy_guests': heavy, 'covered': bool(covered),
            'window_seconds': window, 'cpu_threshold_percent': config['cpu_threshold_percent'],
            'memory_threshold_percent': config['memory_threshold_percent'],
            'cpu_pending': host['cpu'] >= config['cpu_threshold_percent'] / 100 and not cpu_high,
            'memory_pending': host['mem_used'] / host['mem_total'] >= config['memory_threshold_percent'] / 100 and not memory_high,
            'single_cpu_wait': cpu_high and len(heavy) < 2 and not psi_high}


def selection(guest, host, critical=False):
    relief = host.get('relief', {})
    pressure = critical or (host.get('memory_pressure') or 0) >= 1 or (host.get('cpu_pressure') or 0) >= 20
    if not pressure and not relief.get('memory_ready'):
        if relief.get('single_cpu_wait'):
            return 'CPU load is concentrated in one guest; waiting for sustained CPU PSI before moving it.', 0, 0
        if relief.get('cpu_pending') or relief.get('memory_pending'):
            return 'Waiting for the configured sustained overload window before migration.', 0, 0
    cpu = guest['cpu'] or 0
    busy_cores = cpu * guest['cpu_count']
    io = sum(guest.get(key) or 0 for key in telemetry.RATES)
    if relief.get('cpu_ready') and not relief.get('memory_ready'):
        heavy = relief.get('heavy_guests', [])
        priority = 10 if len(heavy) >= 2 and guest['resource'] in heavy[:2] else 0
        return None, priority + min(15, 30 * busy_cores / host['cores']), 0
    if relief.get('memory_ready') and not relief.get('cpu_ready'):
        idle = cpu <= .10 and io < 10 * 1024 ** 2
        return None, 10 if idle else -min(30, 25 * cpu + io / (20 * 1024 ** 2)), 0 if idle else 1
    return None, 0, 0
