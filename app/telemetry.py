import asyncio
import ipaddress
import json
import logging
import math
import time
from contextlib import suppress

from .proxmox import ProxmoxClient, ProxmoxError, segment
from . import balloon, block_io

INTERVAL = 15
GUEST_CONFIG_TTL = 300
SUMMARY_INTERVAL = 60
RAW_RETENTION = 86400
RETENTION = 7 * 86400
COLLECTION_TIMEOUT = 12
PSI = tuple(f'pressure{resource}{kind}' for resource in ('cpu', 'io', 'memory') for kind in ('some', 'full'))
COUNTERS = ('diskread', 'diskwrite', 'netin', 'netout')
RATES = ('disk_read_bps', 'disk_write_bps', 'net_in_bps', 'net_out_bps')
BALLOON_STATS = ('actual', 'free_mem', 'total_mem', 'max_mem', 'last_update', 'mem_swapped_in',
                 'mem_swapped_out', 'major_page_faults', 'minor_page_faults')
BLOCK_STATS = ('rd_bytes', 'wr_bytes', 'rd_operations', 'wr_operations', 'flush_operations',
               'rd_total_time_ns', 'wr_total_time_ns', 'flush_total_time_ns',
               'failed_rd_operations', 'failed_wr_operations', 'failed_flush_operations',
               'invalid_rd_operations', 'invalid_wr_operations', 'invalid_flush_operations')
METRICS = ('cpu', 'cpu_count', 'mem_used', 'mem_total', 'mem_host', 'disk_used', 'disk_total',
           'swap_used', 'swap_total', 'ksm_shared', 'balloon', 'balloon_reclaimed', 'iowait',
           'load1', 'load5', 'load15', 'uptime', *COUNTERS, *RATES, *PSI, *block_io.METRICS)
SCHEMA = f'''
CREATE TABLE IF NOT EXISTS metric_samples (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, sampled_at INTEGER NOT NULL, observed_at REAL NOT NULL,
    node TEXT, status TEXT, quality TEXT NOT NULL, source_at REAL, metadata TEXT NOT NULL,
    {','.join(f'{key} REAL' for key in METRICS)},
    PRIMARY KEY(connection_id, resource, sampled_at)
);
CREATE INDEX IF NOT EXISTS metric_samples_expiry ON metric_samples(sampled_at);
CREATE TABLE IF NOT EXISTS metric_observations (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, sampled_at INTEGER NOT NULL, observed_at REAL NOT NULL,
    node TEXT, status TEXT, quality TEXT NOT NULL, source_at REAL, metadata TEXT NOT NULL,
    {','.join(f'{key} REAL' for key in METRICS)},
    PRIMARY KEY(connection_id, resource, sampled_at)
);
CREATE INDEX IF NOT EXISTS metric_observations_expiry ON metric_observations(sampled_at);
CREATE TABLE IF NOT EXISTS collector_status (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    started_at REAL, finished_at REAL, last_error TEXT, sample_count INTEGER DEFAULT 0
);
'''


def upgrade(db):
    for table in ('metric_observations', 'metric_samples'):
        columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        for key in block_io.METRICS:
            if key not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {key} REAL')


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def numeric_fields(data, fields):
    if not isinstance(data, dict):
        return {}
    return {key: numeric(data[key]) for key in fields if key in data}


def container_memory(sample):
    if sample['resource'].startswith('lxc/') and sample['mem_host'] is None:
        sample['mem_host'] = sample['mem_used']
        sample['metadata']['host_memory_source'] = 'container cgroup memory (mem)'
        summary = sample['metadata'].get('summary', {})
        for key in ('counts', 'max'):
            values = summary.get(key, {})
            if 'mem_used' in values:
                values['mem_host'] = values['mem_used']
    return sample


def normalize(resource, detail, observed_at, errors=None, rrd=None):
    kind = resource['type']
    result = {key: None for key in METRICS}
    result.update(resource=resource['id'], node=resource.get('node'), observed_at=observed_at,
                  status=detail.get('status', resource.get('status')), source_at=None,
                  quality='partial' if errors else 'ok', metadata={'errors': errors or [], 'name': resource.get('name', resource.get('node'))})
    result['metadata']['inventory'] = {key: resource[key] for key in ('id', 'type', 'vmid', 'node', 'name', 'template', 'lock', 'status') if key in resource}
    result['metadata']['inventory'].update({key: detail[key] for key in ('status', 'qmpstatus', 'lock', 'ha') if key in detail})
    result['cpu'] = numeric(detail.get('cpu'))
    result['uptime'] = numeric(detail.get('uptime'))
    if kind == 'node':
        memory, root, swap = (detail.get(k, {}) for k in ('memory', 'rootfs', 'swap'))
        result.update(cpu_count=numeric(detail.get('cpuinfo', {}).get('cpus')),
                      mem_used=numeric(memory.get('used')), mem_total=numeric(memory.get('total')),
                      disk_used=numeric(root.get('used')), disk_total=numeric(root.get('total')),
                      swap_used=numeric(swap.get('used')), swap_total=numeric(swap.get('total')),
                      ksm_shared=numeric(detail.get('ksm', {}).get('shared')), iowait=numeric(detail.get('wait')))
        for key, value in zip(('load1', 'load5', 'load15'), detail.get('loadavg', [])):
            result[key] = numeric(value)
        result['metadata']['disk_io_scope'] = 'guest aggregate'
        result['metadata']['balloon_config'] = detail.get('balloon_config', {})
        if rrd:
            result['source_at'] = numeric(rrd.get('time'))
            for key in PSI:
                result[key] = numeric(rrd.get(key))
            result['net_in_bps'], result['net_out_bps'] = numeric(rrd.get('netin')), numeric(rrd.get('netout'))
            result['metadata']['network_psi_source'] = 'Proxmox RRD minute average'
    else:
        for target, source in (('cpu_count', 'cpus'), ('mem_used', 'mem'), ('mem_total', 'maxmem'),
                               ('mem_host', 'memhost'), ('disk_used', 'disk'), ('disk_total', 'maxdisk'),
                               ('swap_used', 'swap'), ('swap_total', 'maxswap'), ('balloon', 'balloon')):
            result[target] = numeric(detail.get(source))
        for key in (*COUNTERS, *PSI):
            result[key] = numeric(detail.get(key))
        result['metadata'].update(psi_scope='host cgroup', psi_window_seconds=10)
        config = detail.get('guest_config') or {}
        limit, units = numeric(config.get('cpulimit')), numeric(config.get('cpuunits'))
        result['metadata'].update(cpu_limit=limit if limit else None, cpu_units=units)
        if kind == 'qemu':
            balloon_stats = numeric_fields(detail.get('ballooninfo'), BALLOON_STATS)
            result['metadata']['memory_source'] = 'guest balloon statistics' if all(balloon_stats.get(k) is not None for k in ('total_mem', 'free_mem')) else 'host cgroup fallback'
            result['metadata']['balloon_stats'] = balloon_stats
            result['metadata']['balloon_control'] = {'minimum': numeric(detail.get('balloon_min')), 'shares': numeric(detail.get('shares'))}
            blockstat = detail.get('blockstat')
            result['metadata']['block_io'] = {device: numeric_fields(values, BLOCK_STATS) for device, values in blockstat.items()} if isinstance(blockstat, dict) else {}
            result['metadata']['disk_usage_scope'] = 'boot disk capacity only'
            result['disk_used'] = None
        if result['balloon'] and result['mem_total'] is not None:
            result['balloon_reclaimed'] = max(0, result['mem_total'] - result['balloon'])
        if detail.get('status') == 'stopped':
            result['quality'] = 'stopped'
            for key in RATES:
                result[key] = 0.0
    if not detail:
        result['quality'] = 'unavailable'
    return container_memory(result)


def calculate_rates(sample, previous):
    if sample['quality'] in ('stopped', 'unavailable') or sample['resource'].startswith('node/'):
        return
    if not previous or previous['node'] != sample['node'] or previous['quality'] in ('unavailable', 'stopped'):
        return
    elapsed = sample['observed_at'] - previous['observed_at']
    if not 0 < elapsed <= INTERVAL * 3:
        return
    if sample['uptime'] is not None and previous['uptime'] is not None:
        uptime_delta = sample['uptime'] - previous['uptime']
        if uptime_delta < 0 or abs(uptime_delta - elapsed) > 20:
            return
    for counter, rate in zip(COUNTERS, RATES):
        current, old = sample[counter], previous[counter]
        if current is not None and old is not None and current >= old:
            sample[rate] = (current - old) / elapsed
    if sample['resource'].startswith('qemu/'):
        block_io.calculate(sample, previous, elapsed)


def total_known(samples, key):
    values = [s[key] for s in samples if s[key] is not None]
    return sum(values) if values else None


def aggregate(samples, timestamp):
    nodes = [s for s in samples if s['resource'].startswith('node/')]
    guests = [s for s in samples if s['resource'].startswith(('qemu/', 'lxc/'))]
    for node in nodes:
        members = [g for g in guests if g['node'] == node['node']]
        coverage = {}
        for key in ('disk_read_bps', 'disk_write_bps'):
            coverage[key] = {'known': sum(g[key] is not None for g in members), 'total': len(members)}
            node[key] = sum(g[key] for g in members) if coverage[key]['known'] == len(members) and node['quality'] != 'unavailable' else None
            if coverage[key]['known'] < len(members) and node['quality'] == 'ok':
                node['quality'] = 'partial'
        node['metadata']['disk_io_coverage'] = coverage
    dc = {key: None for key in METRICS}
    dc.update(resource='datacenter', node=None, observed_at=timestamp, status='online', source_at=None,
              quality='ok', metadata={'disk_io_scope': 'guest aggregate', 'psi_scope': 'worst node', 'coverage': {}})
    for key in ('cpu_count', 'mem_used', 'mem_total', 'swap_used', 'swap_total', 'ksm_shared', *RATES):
        dc[key] = total_known(nodes, key)
    for key in ('disk_read_bps', 'disk_write_bps'):
        if any(n[key] is None for n in nodes):
            dc[key] = None
    weighted = [(n['cpu'], n['cpu_count']) for n in nodes if n['cpu'] is not None and n['cpu_count']]
    dc['cpu'] = sum(cpu * count for cpu, count in weighted) / sum(count for _, count in weighted) if weighted else None
    for key in PSI:
        values = [n[key] for n in nodes if n[key] is not None]
        dc[key] = max(values) if values else None
    for key in ('cpu', 'mem_used', *RATES, *PSI):
        count = sum(n[key] is not None for n in nodes)
        dc['metadata']['coverage'][key] = {'known': count, 'total': len(nodes)}
        if count < len(nodes) and key != 'pressurecpufull':
            dc['quality'] = 'partial'
    for key in ('balloon', 'balloon_reclaimed'):
        dc[key] = total_known(guests, key)
    dc['metadata'].update(hosts=len(nodes), guests=len(guests), running=sum(g['status'] == 'running' for g in guests))
    return samples + [dc]


def latest_rrd(rows, now):
    eligible = [r for r in rows if numeric(r.get('time')) is not None and 0 <= now - float(r['time']) <= 180
                and any(r.get(k) is not None for k in ('cpu', 'netin', 'pressurecpusome'))]
    return max(eligible, key=lambda r: float(r['time'])) if eligible else None


async def collect_resources(client, rrd_cache=None):
    rrd_cache = {} if rrd_cache is None else rrd_cache
    resources = await client.get('cluster/resources')
    resources = [r for r in resources if r.get('type') in ('node', 'qemu', 'lxc')]
    offline_nodes = {r['node'] for r in resources if r['type'] == 'node' and r.get('status') != 'online'}
    semaphore = asyncio.Semaphore(8)

    async def collect(resource):
        errors, detail, rrd = [], {}, None
        async with semaphore:
            if resource.get('node') in offline_nodes:
                return normalize(resource, {}, time.time(), ['Host is offline.'])
            base = f'nodes/{segment(resource["node"])}'
            path = base + ('/status' if resource['type'] == 'node' else f'/{resource["type"]}/{resource["vmid"]}/status/current')
            try:
                detail = await client.get(path)
                if not isinstance(detail, dict):
                    raise ProxmoxError('Invalid status response.')
            except ProxmoxError as exc:
                errors.append(str(exc)); detail = {}
            observed = time.time()
            if resource['type'] != 'node':
                config_key = base + f'/{resource["type"]}/{resource["vmid"]}/config'
                cached_config = rrd_cache.get(config_key)
                if not cached_config or not 0 <= observed - cached_config[0] < GUEST_CONFIG_TTL:
                    try:
                        config = await client.get(config_key)
                        cached_config = (observed, config if isinstance(config, dict) else {})
                    except ProxmoxError:
                        cached_config = (observed, cached_config[1] if cached_config else {})
                    rrd_cache[config_key] = cached_config
                if detail:
                    detail['guest_config'] = cached_config[1]
            if resource['type'] == 'node':
                config_key = base + '/config'
                cached_config = rrd_cache.get(config_key)
                if not cached_config or not 0 <= observed - cached_config[0] < balloon.CONFIG_TTL:
                    try:
                        config = await client.get(config_key)
                        if not isinstance(config, dict):
                            raise ProxmoxError('Invalid node configuration response.')
                        config = balloon.node_config(config, observed)
                    except ProxmoxError as exc:
                        config = {'target_percent': None, 'checked_at': observed, 'error': str(exc)}
                    cached_config = (observed, config)
                    rrd_cache[config_key] = cached_config
                if detail:
                    detail['balloon_config'] = cached_config[1]
                try:
                    cached = rrd_cache.get(base)
                    if not cached or not 0 <= observed - cached[0] < SUMMARY_INTERVAL:
                        rows = await client.get(base + '/rrddata?timeframe=hour&cf=AVERAGE')
                        cached = (observed, rows)
                        rrd_cache[base] = cached
                    rrd = latest_rrd(cached[1], observed)
                    if not rrd:
                        errors.append('No recent host RRD data available.')
                except ProxmoxError as exc:
                    errors.append(str(exc))
            return normalize(resource, detail, observed, errors, rrd)
    return await asyncio.gather(*(collect(r) for r in resources))


def summarize(rows):
    samples = [sample_dict(row) for row in rows]
    result = dict(samples[-1])
    result['metadata'] = dict(result['metadata'])
    counts, peaks = {}, {}
    last_values = {'cpu_count', 'mem_total', 'disk_total', 'swap_total', 'uptime', *COUNTERS}
    for key in METRICS:
        values = [s[key] for s in samples if s[key] is not None]
        counts[key] = len(values)
        if values:
            peaks[key] = max(values)
        result[key] = samples[-1][key] if key in last_values else sum(values) / len(values) if values else None
    busy = [s['cpu'] * s['cpu_count'] for s in samples if s['cpu'] is not None and s['cpu_count']]
    weights = block_io.summarize(samples, result)
    result['metadata']['summary'] = {
        'version': 1, 'sample_count': len(samples), 'expected_samples': SUMMARY_INTERVAL // INTERVAL,
        'counts': counts, 'max': peaks, 'weights': weights, 'coverage': len(samples) * INTERVAL / SUMMARY_INTERVAL,
        'first_observed_at': samples[0]['observed_at'], 'last_observed_at': samples[-1]['observed_at'],
        'nodes': sorted({s['node'] for s in samples if s['node']}),
        'statuses': sorted({s['status'] for s in samples if s['status']}),
        'source_observations': len({s['source_at'] for s in samples if s['source_at'] is not None}),
        'cpu_busy_cores_mean': sum(busy) / len(busy) if busy else None,
        'cpu_busy_cores_max': max(busy) if busy else None,
    }
    if len(samples) < SUMMARY_INTERVAL // INTERVAL or any(s['quality'] not in ('ok', 'stopped') for s in samples):
        result['quality'] = 'partial' if any(s['quality'] != 'unavailable' for s in samples) else 'unavailable'
    return result


def store_samples(db, connection_id, samples, started_at, inventory_complete=False):
    bucket = int(started_at // INTERVAL) * INTERVAL
    minute = int(started_at // SUMMARY_INTERVAL) * SUMMARY_INTERVAL
    for sample in samples:
        previous = db.execute('SELECT * FROM metric_observations WHERE connection_id=? AND resource=? AND sampled_at<? ORDER BY sampled_at DESC LIMIT 1',
                              (connection_id, sample['resource'], bucket)).fetchone()
        calculate_rates(sample, previous)
    samples = aggregate(samples, started_at)
    if inventory_complete:
        next(s for s in samples if s['resource'] == 'datacenter')['metadata']['inventory_complete'] = True
    columns = ('connection_id', 'resource', 'sampled_at', 'observed_at', 'node', 'status', 'quality', 'source_at', 'metadata', *METRICS)
    for sample in samples:
        values = {**sample, 'connection_id': connection_id, 'sampled_at': bucket, 'metadata': json.dumps(sample['metadata'], separators=(',', ':'))}
        inserted = db.execute(f'INSERT OR IGNORE INTO metric_observations ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', [values[key] for key in columns]).rowcount
        if not inserted:
            continue
        rows = db.execute('SELECT * FROM metric_observations WHERE connection_id=? AND resource=? AND sampled_at>=? AND sampled_at<? ORDER BY sampled_at',
                          (connection_id, sample['resource'], minute, minute + SUMMARY_INTERVAL)).fetchall()
        summary = summarize(rows)
        summary.update(sampled_at=minute, metadata=json.dumps(summary['metadata'], separators=(',', ':')))
        db.execute(f'INSERT OR REPLACE INTO metric_samples ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', [summary[key] for key in columns])
    return len(samples)


def prune(db, now):
    db.execute('DELETE FROM metric_samples WHERE sampled_at<?', (int(now) - RETENTION,))
    db.execute('DELETE FROM metric_observations WHERE sampled_at<?', (int(now) - RAW_RETENTION,))


class Collector:
    def __init__(self, database, config_for, client_factory=ProxmoxClient, after_tick=None):
        self.database, self.config_for, self.client_factory = database, config_for, client_factory
        self.task = None
        self.rrd_caches = {}
        self.after_tick = after_tick

    def start(self):
        self.task = asyncio.create_task(self.run(), name='resource-metrics')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def collect_connection(self, connection_id, started_at):
        config = self.config_for(connection_id)
        cache_key = (connection_id, config.url, config.token_id, config.token_secret, config.verify_tls)
        self.rrd_caches = {key: value for key, value in self.rrd_caches.items() if key[0] != connection_id or key == cache_key}
        rrd_cache = self.rrd_caches.setdefault(cache_key, {})
        with self.database() as db:
            db.execute('INSERT INTO collector_status(connection_id,started_at) VALUES (?,?) ON CONFLICT(connection_id) DO UPDATE SET started_at=excluded.started_at', (connection_id, started_at))
        try:
            async with asyncio.timeout(COLLECTION_TIMEOUT):
                async with self.client_factory(config) as client:
                    samples = await collect_resources(client, rrd_cache)
            with self.database() as db:
                row = db.execute('SELECT url,token_id FROM connections WHERE id=?', (connection_id,)).fetchone()
                if not row or row['url'] != config.url or row['token_id'] != config.token_id:
                    return
                count = store_samples(db, connection_id, samples, started_at, inventory_complete=True)
                errors = sum(bool(s['metadata'].get('errors')) for s in samples)
                db.execute('UPDATE collector_status SET finished_at=?,last_error=?,sample_count=? WHERE connection_id=?',
                           (time.time(), f'{errors} resources have incomplete data.' if errors else None, count, connection_id))
        except (ProxmoxError, TimeoutError) as exc:
            with self.database() as db:
                db.execute('UPDATE collector_status SET finished_at=?,last_error=?,sample_count=0 WHERE connection_id=?',
                           (time.time(), str(exc) or 'Collection exceeded the 12-second deadline.', connection_id))

    async def tick(self, now=None):
        now = time.time() if now is None else now
        with self.database() as db:
            prune(db, now)
            ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
        async def safe_collect(connection_id):
            try:
                await self.collect_connection(connection_id, now)
            except Exception:
                logging.getLogger(__name__).error('Collection failed for connection %s.', connection_id)
        await asyncio.gather(*(safe_collect(i) for i in ids))
        self.rrd_caches = {key: value for key, value in self.rrd_caches.items() if key[0] in ids}
        if self.after_tick:
            await asyncio.to_thread(self.after_tick, time.time())

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).error('Metrics collection cycle failed.')
            await asyncio.sleep(INTERVAL - time.time() % INTERVAL)


def sample_dict(row):
    result = dict(row)
    result['metadata'] = json.loads(result['metadata'])
    return container_memory(result)


def history(db, connection_id, resource, hours=24, now=None):
    now = time.time() if now is None else now
    rows = db.execute('SELECT * FROM metric_samples WHERE connection_id=? AND resource=? AND sampled_at>=? ORDER BY sampled_at',
                      (connection_id, resource, max(int(now - hours * 3600), int(now - RETENTION)))).fetchall()
    samples = [sample_dict(r) for r in rows]
    for sample in samples:
        summary = sample['metadata'].get('summary')
        if summary:
            summary['complete'] = sample['sampled_at'] + SUMMARY_INTERVAL <= now
    return {'resource': resource, 'interval_seconds': SUMMARY_INTERVAL, 'sampling_interval_seconds': INTERVAL,
            'raw_retention_hours': 24, 'retention_days': 7, 'samples': samples}


def latest(db, connection_id, now):
    cutoff = int(now) - RAW_RETENTION
    inventory = db.execute('''SELECT sampled_at,metadata FROM metric_observations
        WHERE connection_id=? AND resource='datacenter' AND sampled_at BETWEEN ? AND ?
        ORDER BY sampled_at DESC LIMIT 1''', (connection_id, cutoff, now)).fetchone()
    if inventory and json.loads(inventory['metadata']).get('inventory_complete'):
        cutoff = inventory['sampled_at']
    rows = db.execute('''SELECT m.* FROM metric_observations m JOIN (
        SELECT resource,MAX(sampled_at) AS latest FROM metric_observations
        WHERE connection_id=? AND sampled_at>=? AND sampled_at<=? GROUP BY resource
    ) last ON m.resource=last.resource AND m.sampled_at=last.latest WHERE m.connection_id=?''',
                      (connection_id, cutoff, now, connection_id)).fetchall()
    return [sample_dict(row) for row in rows]


def addresses(data):
    interfaces = data.get('result', []) if isinstance(data, dict) else data
    result = []
    for interface in interfaces or []:
        for entry in interface.get('ip-addresses', []):
            value = entry.get('ip-address', '')
            try:
                address = ipaddress.ip_address(value)
                if not (address.is_loopback or address.is_link_local or address.is_unspecified):
                    result.append({'address': value, 'interface': interface.get('name', ''), 'prefix': entry.get('prefix')})
            except ValueError:
                pass
    return result


async def resource_detail(client, kind, identifier):
    resources = await client.get('cluster/resources')
    resource_id = f'{kind}/{identifier}'
    resource = next((r for r in resources if r.get('id') == resource_id), None)
    if not resource:
        raise ProxmoxError('Resource is no longer visible in this cluster.')
    base = f'nodes/{segment(resource["node"])}'
    if kind != 'node':
        base += f'/{kind}/{resource["vmid"]}'
    detail = await client.get(base + ('/status' if kind == 'node' else '/status/current'))
    errors, extra = [], {}
    if kind == 'node':
        try:
            repo = await client.get(base + '/apt/repositories')
            extra['repositories'] = {'standard': repo.get('standard-repos', []), 'errors': repo.get('errors', []),
                                      'warnings': [i.get('message') for i in repo.get('infos', []) if i.get('kind') != 'origin']}
        except ProxmoxError as exc:
            errors.append(f'Repository status: {exc}')
    elif detail.get('status') == 'running':
        try:
            extra['ips'] = addresses(await client.get(base + ('/agent/network-get-interfaces' if kind == 'qemu' else '/interfaces')))
        except ProxmoxError as exc:
            extra['ips'] = None
            errors.append(f'IP addresses: {exc}')
    else:
        extra['ips'] = None
        errors.append('IP addresses are unavailable while the guest is stopped.')
    return {'resource': resource, 'detail': detail, 'extra': extra, 'errors': errors, 'observed_at': time.time()}
