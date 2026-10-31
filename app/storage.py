import asyncio
import json
import logging
import re
import time
from contextlib import suppress

from .proxmox import ProxmoxClient, ProxmoxError, segment

INTERVAL = 60
MAX_AGE = 120
SCHEMA = '''
CREATE TABLE IF NOT EXISTS guest_storage (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, node TEXT NOT NULL, checked_at REAL NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(connection_id, resource)
);
'''
DRIVE = re.compile(r'^(?:(?:scsi|sata|virtio|ide|efidisk|tpmstate|unused|mp|dev)\d+|rootfs|vmstate)$')
VOLUME = re.compile(r'^([A-Za-z][A-Za-z0-9_-]*):(.+)$')


def flag(value):
    if value in (True, 1, '1'):
        return True
    if value in (False, 0, '0'):
        return False
    return None


def references(config):
    result = []
    for device, value in sorted(config.items()):
        if value and (device == 'lxc' or device.startswith('lxc.')):
            result.append((device, ''))
            continue
        if not DRIVE.fullmatch(device) and device != 'hookscript':
            continue
        parts = value.split(',') if isinstance(value, str) else []
        volume = next((part.split('=', 1)[1] for part in parts if part.startswith(('file=', 'volume=', 'path='))),
                      parts[0] if parts and '=' not in parts[0] else '')
        if volume == 'none':
            continue
        result.append((device, volume))
    if config.get('cicustom'):
        for item in str(config['cicustom']).split(','):
            key, _, value = item.partition('=')
            result.append((f'cicustom/{key}', value))
    return result


def inspect(config, node, inventories):
    disks, reasons = [], []
    for device, volume in references(config):
        match = VOLUME.fullmatch(volume)
        storage_id = match[1] if match else None
        pool = (inventories.get(node) or {}).get(storage_id)
        state, reason = 'unknown', 'Storage could not be verified.'
        if device == 'lxc' or device.startswith('lxc.'):
            state, reason = 'unknown', 'Custom LXC configuration requires manual migration review.'
        elif volume.startswith('/') or volume == 'cdrom':
            state, reason = 'local', 'Host path or physical device requires manual migration review.'
        elif pool and flag(pool.get('shared')) is False:
            state, reason = 'local', 'Local storage requires disk transfer; excluded from automatic live placement.'
        elif pool and flag(pool.get('shared')) is True:
            if flag(pool.get('active')) is True and flag(pool.get('enabled')) is True:
                state, reason = 'shared', None
            else:
                state, reason = 'unavailable', 'Shared storage is disabled or unavailable on the source.'
        disk = {'device': device, 'volume': volume, 'storage': storage_id, 'state': state, 'reason': reason}
        disks.append(disk)
        if reason:
            reasons.append(f'{device} ({volume or "unrecognized volume"}): {reason}')
    allowed_nodes, blocked_targets = [], {}
    for target, inventory in inventories.items():
        failures = []
        for disk in disks:
            pool = (inventory or {}).get(disk['storage'])
            if not pool or any(flag(pool.get(key)) is not True for key in ('shared', 'active', 'enabled')):
                failures.append(f'{disk["device"]}: shared storage {disk["storage"] or disk["volume"]} is unavailable or unverified.')
        if failures:
            blocked_targets[target] = failures
        elif inventory is not None:
            allowed_nodes.append(target)
    state = 'local' if any(d['state'] == 'local' for d in disks) else 'unknown' if reasons else 'shared' if disks else 'diskless'
    return {'state': state, 'disks': disks, 'reasons': reasons, 'allowed_nodes': sorted(allowed_nodes),
            'blocked_targets': blocked_targets, 'source': 'Proxmox current guest configuration and node storage status'}


def unknown(reason):
    return {'state': 'unknown', 'disks': [], 'reasons': [reason], 'allowed_nodes': [], 'blocked_targets': {}}


def checks(db, connection_id, now):
    result = {}
    for row in db.execute('SELECT * FROM guest_storage WHERE connection_id=?', (connection_id,)):
        data = json.loads(row['data'])
        if not 0 <= now - row['checked_at'] <= MAX_AGE:
            data = {**data, 'state': 'unknown', 'reasons': ['Storage inspection is stale; waiting for a fresh check.'], 'allowed_nodes': []}
        result[row['resource']] = {**data, 'node': row['node'], 'checked_at': row['checked_at']}
    return result


def block_reason(check, node, target=None):
    if not check or check['node'] != node:
        return 'Storage not verified on the current host; waiting for disk inspection.'
    if check['state'] not in ('shared', 'diskless'):
        return 'Storage blocked: ' + ' '.join(check['reasons'])
    if target is not None and target not in check['allowed_nodes']:
        reasons = check.get('blocked_targets', {}).get(target, ['Target storage visibility could not be verified.'])
        return 'Storage blocked: ' + ' '.join(reasons)
    return None


async def collect(client):
    checked_at = time.time()
    resources = await client.get('cluster/resources')
    if not isinstance(resources, list):
        raise ProxmoxError('Invalid storage inventory response.')
    nodes = {r['node']: r.get('status') == 'online' for r in resources if r.get('type') == 'node'}
    guests = [r for r in resources if r.get('type') in ('qemu', 'lxc')]
    semaphore = asyncio.Semaphore(8)

    async def read(path):
        async with semaphore:
            try:
                async with asyncio.timeout(3):
                    return await client.get(path), None
            except (ProxmoxError, TimeoutError) as exc:
                return None, str(exc) or 'Storage inspection timed out.'

    async def node_storage(node):
        if not nodes[node]:
            return node, None
        data, _ = await read(f'nodes/{segment(node)}/storage')
        if not isinstance(data, list) or any(not isinstance(r, dict) or not isinstance(r.get('storage'), str) for r in data):
            return node, None
        return node, {r['storage']: {k: r.get(k) for k in ('shared', 'active', 'enabled')} for r in data}

    inventories = dict(await asyncio.gather(*(node_storage(node) for node in nodes)))

    async def guest_storage(guest):
        node, kind, vmid = guest['node'], guest['type'], guest['vmid']
        if not nodes.get(node):
            data = unknown('Host is offline or its state is unknown.')
        else:
            config, error = await read(f'nodes/{segment(node)}/{kind}/{vmid}/config?current=1')
            if error or not isinstance(config, dict) or not isinstance(config.get('digest'), str):
                data = unknown(error or 'Guest disk configuration could not be verified.')
            else:
                data = inspect(config, node, inventories)
        return {'resource': f'{kind}/{vmid}', 'node': node, 'checked_at': checked_at, 'data': data}

    return await asyncio.gather(*(guest_storage(guest) for guest in guests))


class Worker:
    def __init__(self, database, config_for, client_factory=ProxmoxClient):
        self.database, self.config_for, self.client_factory = database, config_for, client_factory
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name='storage-inspection')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def collect_connection(self, connection_id):
        with self.database() as db:
            identity = db.execute('SELECT url,token_id,secret,verify_tls FROM connections WHERE id=?', (connection_id,)).fetchone()
        if identity is None:
            return
        config, error, rows = self.config_for(connection_id), None, []
        try:
            async with asyncio.timeout(30):
                async with self.client_factory(config) as client:
                    rows = await collect(client)
        except (ProxmoxError, TimeoutError) as exc:
            error = str(exc) or 'Storage inspection exceeded its deadline.'
        with self.database() as db:
            current = db.execute('SELECT url,token_id,secret,verify_tls FROM connections WHERE id=?', (connection_id,)).fetchone()
            if current is None or tuple(current) != tuple(identity):
                return
            if error:
                db.execute('UPDATE guest_storage SET data=?,checked_at=? WHERE connection_id=?',
                           (json.dumps(unknown(error)), time.time(), connection_id))
                return
            db.execute('DELETE FROM guest_storage WHERE connection_id=?', (connection_id,))
            db.executemany('INSERT INTO guest_storage VALUES(?,?,?,?,?)',
                           [(connection_id, row['resource'], row['node'], row['checked_at'], json.dumps(row['data'])) for row in rows])

    async def tick(self):
        with self.database() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
        async def safe_collect(connection_id):
            try:
                await self.collect_connection(connection_id)
            except Exception:
                logging.getLogger(__name__).error('Storage inspection failed for connection %s.', connection_id)
        await asyncio.gather(*(safe_collect(identifier) for identifier in ids))

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).error('Storage inspection cycle failed.')
            await asyncio.sleep(INTERVAL - time.time() % INTERVAL)
