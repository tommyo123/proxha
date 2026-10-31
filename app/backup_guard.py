import asyncio
import json

from . import node_maintenance
from .proxmox import ProxmoxError, segment

TTL = 45
SCHEMA = '''CREATE TABLE IF NOT EXISTS backup_status (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    checked_at REAL NOT NULL, document TEXT NOT NULL
);'''


class BackupBlocked(ProxmoxError):
    pass


def save(db, connection_id, status, now):
    db.execute('INSERT OR REPLACE INTO backup_status VALUES(?,?,?)',
               (connection_id, now, json.dumps(status)))


def read(db, connection_id, now):
    row = db.execute('SELECT * FROM backup_status WHERE connection_id=?', (connection_id,)).fetchone()
    if not row or not 0 <= now - row['checked_at'] <= TTL:
        return {'blocked': True, 'state': 'unknown', 'message': 'Waiting for a fresh cluster-wide backup check.', 'checked_at': row['checked_at'] if row else None}
    return {**json.loads(row['document']), 'checked_at': row['checked_at']}


async def inspect(client, resources=None):
    try:
        await node_maintenance.verify_inventory_access(client)
        resources = await client.get('cluster/resources') if resources is None else resources
        nodes = [r['node'] for r in resources if r.get('type') == 'node' and r.get('status') == 'online']
        if not nodes:
            raise ProxmoxError('No online hosts could be verified.')
        rows = await asyncio.gather(*(client.get(f'nodes/{segment(node)}/tasks?source=active&typefilter=vzdump&limit=500') for node in nodes))
        if any(not isinstance(tasks, list) or len(tasks) >= 500 for tasks in rows):
            raise ProxmoxError('The active backup task list is incomplete.')
        active = [{'node': node, 'upid': task.get('upid'), 'guest': task.get('id')}
                  for node, tasks in zip(nodes, rows) for task in tasks
                  if not task.get('endtime') and task.get('status') in (None, 'running')]
        locked = [r.get('id') for r in resources if r.get('lock') == 'backup']
        blocked = bool(active or locked)
        return {'blocked': blocked, 'state': 'backup' if blocked else 'clear', 'tasks': active, 'locked_guests': locked,
                'message': 'Backup is running. New migrations are paused cluster-wide; submitted migrations finish normally.' if blocked else 'No active Proxmox backup tasks detected.'}
    except ProxmoxError as exc:
        return {'blocked': True, 'state': 'unknown', 'message': f'Backup status could not be verified. New migrations are paused: {exc}'}


async def require_clear(client, resources=None):
    status = await inspect(client, resources)
    if status['blocked']:
        raise BackupBlocked(status['message'])
    return status
