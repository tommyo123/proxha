import json


DIRECTIONS = {'read': 'rd', 'write': 'wr'}
METRICS = tuple(f'disk_{direction}_{metric}' for direction in DIRECTIONS for metric in ('iops', 'latency_ms'))


def calculate(sample, previous, elapsed):
    current = sample['metadata'].get('block_io') or {}
    old = json.loads(previous['metadata']).get('block_io') or {}
    if not current or set(current) != set(old):
        return
    weights = sample['metadata']['block_io_weights'] = {}
    for direction, prefix in DIRECTIONS.items():
        deltas = {}
        for field in ('operations', 'total_time_ns'):
            key = f'{prefix}_{field}'
            pairs = [(current[d].get(key), old[d].get(key)) for d in current]
            if all(a is not None and b is not None and a >= b for a, b in pairs):
                deltas[field] = sum(a - b for a, b in pairs)
        operations = deltas.get('operations')
        if operations is None:
            continue
        sample[f'disk_{direction}_iops'] = operations / elapsed
        duration = deltas.get('total_time_ns')
        if operations > 0 and duration is not None:
            key = f'disk_{direction}_latency_ms'
            sample[key] = duration / operations / 1_000_000
            weights[key] = operations


def summarize(samples, result):
    weights = {}
    for direction in DIRECTIONS:
        key = f'disk_{direction}_latency_ms'
        values = [(s[key], s['metadata'].get('block_io_weights', {}).get(key, 0)) for s in samples if s[key] is not None]
        weights[key] = sum(weight for _, weight in values)
        result[key] = sum(value * weight for value, weight in values) / weights[key] if weights[key] else None
    return weights
