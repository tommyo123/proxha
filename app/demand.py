import math
from collections import defaultdict

SMOOTH_SECONDS = 300
PEAK_SECONDS = 3600
PEAK_PERCENTILE = .95
PSI_MAX_AGE = 180
# Mean guest CPU PSI "some" above this share of time indicates waiting for host CPU.
CONTENTION_PSI = 10


def window(db, connection_id, now):
    rows = db.execute('''SELECT resource,sampled_at,node,status,cpu,cpu_count,pressurecpusome FROM metric_observations
        WHERE connection_id=? AND sampled_at>=? AND sampled_at<=? AND quality!='unavailable'
        AND cpu IS NOT NULL AND cpu_count>0 ORDER BY sampled_at''', (connection_id, int(now) - PEAK_SECONDS, now))
    result = defaultdict(list)
    for row in rows:
        result[row['resource']].append((row['sampled_at'], row['node'], row['cpu'] * row['cpu_count'], row['status'], row['pressurecpusome']))
    return result


def contended(profile):
    # Waiting caused by the guest's own CPU limit cannot be relieved by a move.
    psi, limit = (profile or {}).get('cpu_psi') or 0, (profile or {}).get('cpu_limit')
    return psi >= CONTENTION_PSI and not (limit and profile['busy_cores'] >= .9 * limit)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)] if ordered else None


def fresh_psi(sample, now):
    source = sample.get('source_at') if sample.get('source_at') is not None else sample.get('observed_at')
    return sample.get('pressurecpusome') if source is not None and 0 <= now - source <= PSI_MAX_AGE else None


# Guest CPU demand is the five-minute mean of running observations; the reservation
# peak is the one-hour p95, never below the mean.
def guest(sample, rows, now):
    current = sample['cpu'] * sample['cpu_count']
    running = [(at, busy, psi) for at, _, busy, status, psi in rows if status == 'running']
    recent = [(busy, psi) for at, busy, psi in running if at >= now - SMOOTH_SECONDS]
    busy = sum(b for b, _ in recent) / len(recent) if recent else current
    peak = percentile([b for _, b, _ in running], PEAK_PERCENTILE)
    peak = max(busy, peak if peak is not None else current)
    pressure = [p for _, p in recent if p is not None]
    psi = sum(pressure) / len(pressure) if pressure else fresh_psi(sample, now)
    # A CPU limit caps what the guest can use on any host.
    limit = (sample.get('metadata') or {}).get('cpu_limit')
    if limit:
        busy, peak = min(busy, limit), min(peak, limit)
    return busy, peak, psi


# Mean host busy cores over the smoothing window, with each slot rewritten to the
# current guest placement so a completed move is reflected without waiting.
def hosts(history, placements, busy_now, now):
    measured, seen = defaultdict(dict), defaultdict(dict)
    for resource, rows in history.items():
        for at, node, busy, status, *_ in rows:
            if at < now - SMOOTH_SECONDS:
                continue
            if resource.startswith('node/'):
                measured[resource[5:]][at] = busy
            else:
                seen[at][resource] = (node, status == 'running', busy)
    result = {}
    for node, slots in measured.items():
        values = []
        for at, busy in slots.items():
            for resource in seen[at].keys() | placements.keys():
                then = seen[at].get(resource)
                was_here = then is not None and then[0] == node and then[1]
                is_here = placements.get(resource) == node
                if was_here and not is_here:
                    busy -= then[2]
                elif is_here and then is not None and not was_here:
                    # Guests that were elsewhere or stopped count with their load now.
                    busy += then[2] if then[1] else busy_now.get(resource, 0)
            values.append(max(0, busy))
        result[node] = sum(values) / len(values)
    return result
