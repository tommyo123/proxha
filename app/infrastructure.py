import asyncio
import json
import logging
import math
import re
import time
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from prometheus_client.parser import text_string_to_metric_families

SCHEMA = '''
CREATE TABLE IF NOT EXISTS infrastructure_sources (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, config TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(connection_id,node)
);
CREATE TABLE IF NOT EXISTS infrastructure_samples (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, sampled_at INTEGER NOT NULL, raw TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(connection_id,node,sampled_at)
);
CREATE INDEX IF NOT EXISTS infrastructure_samples_expiry ON infrastructure_samples(sampled_at);
CREATE TABLE IF NOT EXISTS infrastructure_minutes (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, sampled_at INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(connection_id,node,sampled_at)
);
CREATE INDEX IF NOT EXISTS infrastructure_minutes_expiry ON infrastructure_minutes(sampled_at);
'''
METRICS = ('disk_latency_ms', 'disk_queue_depth', 'disk_iops', 'disk_busy_percent', 'network_utilization_percent',
           'network_drop_percent', 'network_error_percent', 'network_rx_bps', 'network_tx_bps', 'network_capacity_bps')
logger = logging.getLogger(__name__)
DISK = ('reads_completed_total', 'writes_completed_total', 'read_time_seconds_total', 'write_time_seconds_total',
        'io_time_weighted_seconds_total', 'io_time_seconds_total')
NET = ('receive_bytes_total', 'transmit_bytes_total', 'receive_packets_total', 'transmit_packets_total',
       'receive_drop_total', 'transmit_drop_total', 'receive_errs_total', 'transmit_errs_total', 'speed_bytes', 'up')


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=False, strict=True)
    url: str = ''
    disks: list[str] = Field(default_factory=list, max_length=64)
    interfaces: list[str] = Field(default_factory=list, max_length=32)
    guard_enabled: bool = Field(default=True, strict=True)
    disk_latency_ms: float = Field(default=20, ge=1, le=5000)
    disk_queue_depth: float = Field(default=2, ge=.1, le=1000)
    network_utilization_percent: float = Field(default=90, ge=20, le=100)
    network_drop_percent: float = Field(default=1, ge=.01, le=100)
    revision: int = Field(default=0, ge=0, strict=True)

    @field_validator('disks', 'interfaces')
    @classmethod
    def valid_devices(cls, value):
        if len(set(value)) != len(value) or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', v) for v in value):
            raise ValueError('Use distinct Linux device names.')
        return value

    @model_validator(mode='after')
    def valid_source(self):
        if self.enabled or self.url:
            p = urlsplit(self.url)
            if p.port is not None and not 1 <= p.port <= 65535:
                raise ValueError('Use a valid exporter port.')
            if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/', '/metrics'):
                raise ValueError('Use an HTTP(S) node_exporter URL without credentials or query parameters.')
        if self.enabled and (not self.disks or not self.interfaces):
            raise ValueError('Select physical disks and physical network interfaces before enabling collection.')
        return self


def sources(db, connection_id):
    return {r['node']: {**json.loads(r['config']), 'revision': r['revision']} for r in db.execute('SELECT * FROM infrastructure_sources WHERE connection_id=?', (connection_id,))}


def parse(text, node, config, now):
    metrics, identity, boot = {}, None, None
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == 'node_uname_info':
                identity = s.labels.get('nodename')
            if s.name == 'node_boot_time_seconds':
                boot = s.value
            device = s.labels.get('device')
            if device in config['disks'] and s.name.removeprefix('node_disk_') in DISK or device in config['interfaces'] and s.name.removeprefix('node_network_') in NET:
                if math.isfinite(s.value) and 0 <= s.value < 1e30:
                    metrics[f'{device}/{s.name}'] = s.value
    if not identity or identity.split('.')[0] != node or boot is None or not math.isfinite(boot) or boot < 0:
        raise ValueError('Exporter identity or boot time is missing or does not match the configured Proxmox host.')
    return {'at': now, 'boot': boot, 'metrics': metrics, 'revision': config['revision']}


def derive(raw, previous, config):
    data = {k: None for k in METRICS}
    data.update(disks=[], interfaces=[], quality='warming', error=None)
    dt = raw['at'] - previous.get('at', 0) if previous else 0
    valid = previous and 0 < dt <= 45 and previous.get('boot') == raw['boot'] and previous.get('revision') == raw['revision']
    def value(device, metric):
        return raw['metrics'].get(f'{device}/{metric}')
    def rate(device, metric):
        key = f'{device}/{metric}'
        a, b = raw['metrics'].get(key), previous.get('metrics', {}).get(key) if previous else None
        return (a - b) / dt if valid and a is not None and b is not None and a >= b else None
    for device in config['disks']:
        r = {k: rate(device, 'node_disk_' + k) for k in DISK}
        known = all(v is not None for v in r.values())
        ops = r['reads_completed_total'] + r['writes_completed_total'] if known else None
        latency = 1000 * (r['read_time_seconds_total'] + r['write_time_seconds_total']) / ops if ops else 0 if known else None
        data['disks'].append({'device': device, 'latency_ms': latency, 'queue_depth': r['io_time_weighted_seconds_total'],
                              'iops': ops, 'busy_percent': min(100, 100 * r['io_time_seconds_total']) if known else None})
    for device in config['interfaces']:
        rates = {k: rate(device, 'node_network_' + k) for k in NET if k.endswith('_total')}
        known = all(v is not None for v in rates.values())
        speed = value(device, 'node_network_speed_bytes')
        up = value(device, 'node_network_up')
        speed = speed if speed and speed < 10**13 else None
        packets = rates['receive_packets_total'] + rates['transmit_packets_total'] if known else None
        drops = rates['receive_drop_total'] + rates['transmit_drop_total'] if known else None
        errors = rates['receive_errs_total'] + rates['transmit_errs_total'] if known else None
        total = packets + drops + errors if known else None
        data['interfaces'].append({'device': device, 'capacity_bps': speed, 'up': up,
            'rx_bps': rates['receive_bytes_total'], 'tx_bps': rates['transmit_bytes_total'],
            'utilization_percent': 100 * max(rates['receive_bytes_total'], rates['transmit_bytes_total']) / speed if known and speed else None,
            'drop_percent': 100 * drops / total if total else 0 if known else None,
            'error_percent': 100 * errors / total if total else 0 if known else None})
    def complete(items, field, fn=max):
        values = [r[field] for r in items]
        return fn(values) if values and all(v is not None for v in values) else None
    for target, field in [('disk_latency_ms', 'latency_ms'), ('disk_queue_depth', 'queue_depth'), ('disk_iops', 'iops'), ('disk_busy_percent', 'busy_percent')]:
        data[target] = complete(data['disks'], field, sum if field == 'iops' else max)
    for target, field in [('network_utilization_percent', 'utilization_percent'), ('network_drop_percent', 'drop_percent'), ('network_error_percent', 'error_percent'),
                          ('network_rx_bps', 'rx_bps'), ('network_tx_bps', 'tx_bps'), ('network_capacity_bps', 'capacity_bps')]:
        data[target] = complete(data['interfaces'], field, max if 'percent' in field else sum)
    data['quality'] = 'ok' if all(data[k] is not None for k in METRICS) and all(i['up'] is not None for i in data['interfaces']) else 'partial' if valid else 'warming'
    return data


def pressure(data, config):
    reasons = []
    for disk in data.get('disks', []):
        if (disk.get('latency_ms') or 0) >= config['disk_latency_ms'] and (disk.get('queue_depth') or 0) >= config['disk_queue_depth']:
            reasons.append(f'{disk["device"]}: disk latency {disk["latency_ms"]:.1f} ms and queue depth {disk["queue_depth"]:.1f}.')
    for nic in data.get('interfaces', []):
        if nic.get('up') == 0:
            reasons.append(f'{nic["device"]}: link is down.')
        if (nic.get('utilization_percent') or 0) >= config['network_utilization_percent']:
            reasons.append(f'{nic["device"]}: network utilization {nic["utilization_percent"]:.1f}%.')
        if max(nic.get('drop_percent') or 0, nic.get('error_percent') or 0) >= config['network_drop_percent']:
            reasons.append(f'{nic["device"]}: interface drops/errors exceed {config["network_drop_percent"]}%.')
    return reasons


def latest(db, connection_id, now):
    result = {}
    for node, config in sources(db, connection_id).items():
        rows = db.execute('''SELECT sampled_at,data FROM infrastructure_samples WHERE connection_id=? AND node=?
            AND sampled_at<=? AND COALESCE(json_extract(raw,'$.at'),sampled_at)<=? ORDER BY sampled_at DESC LIMIT 3''',
            (connection_id, node, now, now)).fetchall()
        data = json.loads(rows[0]['data']) if rows else {'quality': 'unavailable', 'error': 'Waiting for exporter samples.'}
        stale = not rows or not 0 <= now - rows[0]['sampled_at'] <= 45
        recent = [json.loads(r['data']) for r in rows if 0 <= now - r['sampled_at'] <= 45]
        sustained = len(recent) >= 3 and all(pressure(d, config) for d in recent) and rows[0]['sampled_at'] - rows[2]['sampled_at'] >= 25
        reasons = pressure(data, config)
        result[node] = {**data, 'sampled_at': rows[0]['sampled_at'] if rows else None, 'configured': config['enabled'],
                        'stale': stale, 'guard_enabled': config['guard_enabled'], 'sustained': sustained,
                        'block_reason': 'Infrastructure measurements are missing, incomplete or stale.' if config['enabled'] and config['guard_enabled'] and (stale or data['quality'] != 'ok') else
                                        'Infrastructure pressure: ' + ' '.join(reasons) if config['enabled'] and config['guard_enabled'] and sustained else None,
                        'reasons': reasons}
    return result


def store(db, connection_id, node, now, raw, data):
    bucket, minute = int(now // 15) * 15, int(now // 60) * 60
    db.execute('INSERT OR REPLACE INTO infrastructure_samples VALUES(?,?,?,?,?)', (connection_id, node, bucket, json.dumps(raw), json.dumps(data)))
    rows = [json.loads(r['data']) for r in db.execute('SELECT data FROM infrastructure_samples WHERE connection_id=? AND node=? AND sampled_at>=? AND sampled_at<? ORDER BY sampled_at', (connection_id, node, minute, minute + 60))]
    summary = {'sample_count': len(rows), 'mean': {}, 'max': {}, 'counts': {}}
    for k in METRICS:
        values = [d[k] for d in rows if d.get(k) is not None]
        summary['counts'][k] = len(values)
        summary['mean'][k] = sum(values) / len(values) if values else None
        summary['max'][k] = max(values) if values else None
    db.execute('INSERT OR REPLACE INTO infrastructure_minutes VALUES(?,?,?,?)', (connection_id, node, minute, json.dumps(summary)))


def prune(db, now):
    db.execute('DELETE FROM infrastructure_samples WHERE sampled_at<?', (now - 86400,))
    db.execute('DELETE FROM infrastructure_minutes WHERE sampled_at<?', (now - 7 * 86400,))


class Worker:
    def __init__(self, database, client_factory=None):
        self.database = database
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False))
        self.task = None

    async def collect(self, connection_id, node, config):
        now = time.time()
        raw, data = {}, {k: None for k in METRICS}
        try:
            async with self.client_factory() as client, asyncio.timeout(6):
                async with client.stream('GET', config['url'].rstrip('/') + ('/metrics' if not urlsplit(config['url']).path.strip('/') else '')) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024**2:
                            raise ValueError('Exporter response exceeds 4 MiB.')
            raw = await asyncio.to_thread(parse, content.decode('utf-8'), node, config, now)
            with self.database() as db:
                previous = db.execute('SELECT raw FROM infrastructure_samples WHERE connection_id=? AND node=? ORDER BY sampled_at DESC LIMIT 1', (connection_id, node)).fetchone()
            data = derive(raw, json.loads(previous['raw']) if previous else None, config)
        except (httpx.HTTPError, ValueError, TimeoutError, UnicodeError):
            data.update(quality='unavailable', error='Exporter collection failed. Check URL, host identity, TLS and selected devices.')
        with self.database() as db:
            current = sources(db, connection_id).get(node)
            if current == config and current['enabled']:
                store(db, connection_id, node, now, raw, data)

    async def cycle(self):
        with self.database() as db:
            prune(db, time.time())
            rows = [dict(r) for r in db.execute('SELECT * FROM infrastructure_sources')]
        sem = asyncio.Semaphore(4)
        async def one(row):
            config = {**json.loads(row['config']), 'revision': row['revision']}
            if config['enabled']:
                async with sem:
                    await self.collect(row['connection_id'], row['node'], config)
        results = await asyncio.gather(*(one(row) for row in rows), return_exceptions=True)
        if any(isinstance(r, Exception) for r in results):
            logger.warning('An infrastructure collector failed; it will retry next cycle.')

    async def run(self):
        while True:
            try:
                await self.cycle()
            except Exception:
                logger.warning('Infrastructure collection failed; it will retry next cycle.')
            await asyncio.sleep(15 - time.time() % 15)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
