import json
import time
import unicodedata
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .guest_policy import preference


def balancing_types(kind):
    # The LXC schedule balances VMs and containers together.
    return {'qemu': ('qemu',), 'lxc': ('qemu', 'lxc'), 'all': ('qemu', 'lxc')}[kind]


SCHEMA = '''
CREATE TABLE IF NOT EXISTS migration_groups (
    id INTEGER PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name TEXT NOT NULL, name_key TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(connection_id, name_key), UNIQUE(id, connection_id));
CREATE TABLE IF NOT EXISTS migration_group_members (
    group_id INTEGER NOT NULL, connection_id INTEGER NOT NULL, node TEXT NOT NULL,
    PRIMARY KEY(connection_id, node),
    FOREIGN KEY(group_id, connection_id) REFERENCES migration_groups(id, connection_id) ON DELETE CASCADE);
'''


class GroupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    notes: str = Field(default='', max_length=1000)
    enabled: bool = True
    nodes: list[str] = Field(default_factory=list, max_length=256)

    @field_validator('name', 'notes')
    @classmethod
    def clean_text(cls, value):
        value = unicodedata.normalize('NFC', value.strip())
        if any(unicodedata.category(c) == 'Cc' and c not in '\n\t' for c in value):
            raise ValueError('Control characters are not allowed.')
        return value

    @field_validator('name')
    @classmethod
    def nonempty_name(cls, value):
        if not value or '\n' in value or '\t' in value:
            raise ValueError('Enter a group name on one line.')
        return value

    @field_validator('nodes')
    @classmethod
    def unique_nodes(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Select each host only once.')
        return sorted(value)


class GroupUpdate(GroupInput):
    revision: int = Field(ge=1)


def list_groups(db, connection_id):
    groups = [dict(r) for r in db.execute(
        'SELECT id,name,notes,enabled,revision,updated_at FROM migration_groups WHERE connection_id=? ORDER BY name_key',
        (connection_id,))]
    members = db.execute('SELECT group_id,node FROM migration_group_members WHERE connection_id=? ORDER BY node',
                         (connection_id,)).fetchall()
    for group in groups:
        group['enabled'] = bool(group['enabled'])
        group['nodes'] = [r['node'] for r in members if r['group_id'] == group['id']]
    return groups


def group_for(db, connection_id, group_id):
    group = next((g for g in list_groups(db, connection_id) if g['id'] == group_id), None)
    if group is None:
        raise HTTPException(404, 'Migration domain not found.')
    return group


def save_group(db, connection, data, group_id=None):
    connection_id = connection['id']
    previous = group_for(db, connection_id, group_id) if group_id is not None else None
    if previous and previous['revision'] != data.revision:
        raise HTTPException(409, 'This group changed in another session. Refresh and reopen it before saving.')
    snapshot = json.loads(connection['snapshot'] or '{}')
    known = {n['node'] for n in snapshot.get('nodes', [])}
    retained = set(previous['nodes']) if previous else set()
    if set(data.nodes) - known - retained:
        raise HTTPException(422, 'Hosts must belong to this connection. Refresh the cluster inventory first.')
    key = data.name.casefold()
    for other in list_groups(db, connection_id):
        if other['id'] == group_id:
            continue
        if other['name'].casefold() == key:
            raise HTTPException(409, 'A migration domain with this name already exists.')
        if set(data.nodes).intersection(other['nodes']):
            raise HTTPException(409, 'A host can belong to only one migration domain, including disabled domains.')
    now = datetime.now(timezone.utc).isoformat()
    if previous:
        db.execute('UPDATE migration_groups SET name=?,name_key=?,notes=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                   (data.name, key, data.notes, data.enabled, now, group_id))
        db.execute('DELETE FROM migration_group_members WHERE group_id=?', (group_id,))
    else:
        group_id = db.execute('INSERT INTO migration_groups(connection_id,name,name_key,notes,enabled,updated_at) VALUES (?,?,?,?,?,?)',
                              (connection_id, data.name, key, data.notes, data.enabled, now)).lastrowid
    db.executemany('INSERT INTO migration_group_members VALUES (?,?,?)',
                   [(group_id, connection_id, node) for node in data.nodes])
    return group_for(db, connection_id, group_id)


def pair_policy(groups, nodes, source, target, inventory_fresh=True):
    result = {'source': source, 'target': target, 'allowed': False, 'group_id': None}
    if source == target:
        code, reason = 'same_host', 'Choose a different target host.'
    elif not inventory_fresh:
        code, reason = 'stale_inventory', 'Refresh the cluster inventory before evaluating automatic migration.'
    elif source not in nodes or target not in nodes:
        code, reason = 'unknown_host', 'A host is missing from the cluster inventory.'
    elif nodes[source].get('status') != 'online' or nodes[target].get('status') != 'online':
        code, reason = 'offline_host', 'Both hosts must be online.'
    else:
        source_group = next((g for g in groups if source in g['nodes']), None)
        target_group = next((g for g in groups if target in g['nodes']), None)
        if not source_group or not target_group:
            code, reason = 'unassigned', 'Both hosts must belong to a migration domain.'
        elif source_group['id'] != target_group['id']:
            code, reason = 'different_groups', 'Automatic live migration cannot cross group boundaries.'
        elif not source_group['enabled']:
            code, reason = 'disabled_group', 'This migration domain is disabled.'
        else:
            result.update(allowed=True, group_id=source_group['id'])
            code, reason = 'allowed', 'Allowed by group policy. Guest compatibility and capacity checks are still required.'
    return {**result, 'code': code, 'reason': reason}


def automatic_live_policy(groups, nodes, guest, target, inventory_fresh=True, *, stickiness=5,
                          intent='balance', exception_reason=None):
    return automatic_migration_policy(groups, nodes, guest, target, inventory_fresh,
                                      stickiness=stickiness, intent=intent, exception_reason=exception_reason)


def automatic_migration_policy(groups, nodes, guest, target, inventory_fresh=True, *, stickiness=5,
                               intent='balance', exception_reason=None, restart_containers=False):
    kind = guest.get('type')
    if kind not in ('qemu', 'lxc') or guest.get('status') != 'running' or guest.get('qmpstatus') in ('paused', 'suspended'):
        return {'allowed': False, 'code': 'guest_unavailable', 'reason': 'Automatic balancing requires a running VM or container.'}
    if kind == 'lxc' and not restart_containers:
        return {'allowed': False, 'code': 'container_restart_disabled', 'reason': 'Container restart migration is disabled for this operation.'}
    if guest.get('template') or guest.get('lock'):
        return {'allowed': False, 'code': 'guest_unavailable', 'reason': 'Templates and locked guests cannot migrate automatically.'}
    boundary = pair_policy(groups, nodes, guest.get('node'), target, inventory_fresh)
    if not boundary['allowed']:
        return boundary
    movement = preference(stickiness, intent, exception_reason)
    return {**boundary, **movement, 'group_id': boundary['group_id']}


def document(db, connection):
    groups = list_groups(db, connection['id'])
    snapshot = json.loads(connection['snapshot'] or '{}')
    nodes = {n['node']: n for n in snapshot.get('nodes', [])}
    assigned = {node for group in groups for node in group['nodes']}
    synced_at = connection['synced_at']
    age = time.time() - datetime.fromisoformat(synced_at).timestamp() if synced_at else None
    fresh = age is not None and 0 <= age <= 150 and not connection['last_error']
    return {'groups': groups, 'nodes': [{'node': name, 'status': n.get('status', 'unknown')} for name, n in sorted(nodes.items())],
            'unassigned': sorted(set(nodes) - assigned), 'inventory_fresh': bool(fresh), 'synced_at': synced_at,
            'automatic_execution_enabled': False,
            'pairs': [pair_policy(groups, nodes, source, target, fresh) for source in sorted(nodes) for target in sorted(nodes)]}
