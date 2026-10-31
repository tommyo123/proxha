import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = '''
CREATE TABLE IF NOT EXISTS resource_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('guest','host')),
    name TEXT NOT NULL, name_key TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(connection_id,name_key), UNIQUE(id,connection_id,kind)
);
CREATE TABLE IF NOT EXISTS resource_group_members (
    group_id INTEGER NOT NULL, connection_id INTEGER NOT NULL, kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    PRIMARY KEY(group_id,resource),
    CHECK((kind='guest' AND (resource LIKE 'qemu/%' OR resource LIKE 'lxc/%'))
          OR (kind='host' AND resource LIKE 'node/%')),
    FOREIGN KEY(group_id,connection_id,kind) REFERENCES resource_groups(id,connection_id,kind) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS resource_group_members_resource ON resource_group_members(connection_id,resource);
'''


class GroupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['guest', 'host']
    name: str = Field(min_length=1, max_length=80)
    notes: str = Field(default='', max_length=1000)
    members: list[str] = Field(default_factory=list, max_length=10000)

    @field_validator('name', 'notes')
    @classmethod
    def clean_text(cls, value):
        value = unicodedata.normalize('NFC', value.strip())
        if any(unicodedata.category(c) == 'Cc' and c not in '\n\t' for c in value):
            raise ValueError('Control characters are not allowed.')
        return value

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        if not value or '\n' in value or '\t' in value:
            raise ValueError('Enter a group name on one line.')
        return value

    @model_validator(mode='after')
    def valid_members(self):
        pattern = r'(?:qemu|lxc)/[1-9][0-9]{0,9}' if self.kind == 'guest' else r'node/[A-Za-z0-9][A-Za-z0-9._-]{0,252}'
        if any(not re.fullmatch(pattern, value) for value in self.members):
            raise ValueError('Guest groups accept VM/container IDs; host groups accept host IDs only.')
        if len(set(self.members)) != len(self.members):
            raise ValueError('Select each member only once.')
        self.members.sort()
        return self


class GroupUpdate(GroupInput):
    revision: int = Field(ge=1, strict=True)


def inventory(connection):
    snapshot = json.loads(connection['snapshot'] or '{}')
    members = {}
    for node in snapshot.get('nodes', []):
        resource = f'node/{node["node"]}'
        members[resource] = {'resource': resource, 'kind': 'host', 'type': 'node', 'name': node['node'],
                             'node': node['node'], 'status': node.get('status', 'unknown'), 'missing': False}
    for guest in snapshot.get('guests', []):
        if guest.get('type') not in ('qemu', 'lxc'):
            continue
        resource = f'{guest["type"]}/{guest["vmid"]}'
        members[resource] = {'resource': resource, 'kind': 'guest', 'type': guest['type'],
                             'name': guest.get('name') or resource, 'node': guest.get('node'),
                             'status': guest.get('status', 'unknown'), 'missing': False}
    return members


def list_groups(db, connection_id):
    groups = [dict(row) for row in db.execute('SELECT id,kind,name,notes,revision,created_at,updated_at FROM resource_groups WHERE connection_id=? ORDER BY name_key,id', (connection_id,))]
    members = {}
    for row in db.execute('SELECT group_id,resource FROM resource_group_members WHERE connection_id=? ORDER BY resource', (connection_id,)):
        members.setdefault(row['group_id'], []).append(row['resource'])
    for group in groups:
        group['members'] = members.get(group['id'], [])
    return groups


def group_for(db, connection_id, group_id):
    group = next((g for g in list_groups(db, connection_id) if g['id'] == group_id), None)
    if group is None:
        raise HTTPException(404, 'Resource group not found.')
    return group


def document(db, connection):
    available = inventory(connection)
    groups = list_groups(db, connection['id'])
    for group in groups:
        group['missing_members'] = [resource for resource in group['members'] if resource not in available]
    synced = connection['synced_at']
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(synced)).total_seconds() if synced else None
    return {'groups': groups, 'inventory': sorted(available.values(), key=lambda item: (item['kind'], item['name'].casefold(), item['resource'])),
            'synced_at': synced, 'inventory_fresh': age is not None and 0 <= age <= 150 and not connection['last_error'],
            'rules_enforced': bool(db.execute('SELECT 1 FROM placement_rules WHERE connection_id=? AND enabled=1', (connection['id'],)).fetchone())}


def save(db, connection, data, group_id=None):
    connection_id = connection['id']
    previous = group_for(db, connection_id, group_id) if group_id is not None else None
    if previous and previous['revision'] != data.revision:
        raise HTTPException(409, 'This group changed in another session. Refresh and reopen it before saving.')
    if previous and previous['kind'] != data.kind:
        raise HTTPException(422, 'A saved group cannot change type. Create a separate guest or host group.')
    retained = set(previous['members']) if previous else set()
    if set(data.members) - inventory(connection).keys() - retained:
        raise HTTPException(422, 'New members must exist in this cluster inventory. Refresh the inventory first.')
    now = datetime.now(timezone.utc).isoformat()
    try:
        if previous:
            db.execute('UPDATE resource_groups SET name=?,name_key=?,notes=?,revision=revision+1,updated_at=? WHERE id=? AND connection_id=?',
                       (data.name, data.name.casefold(), data.notes, now, group_id, connection_id))
            db.execute('DELETE FROM resource_group_members WHERE group_id=? AND connection_id=?', (group_id, connection_id))
        else:
            group_id = db.execute('INSERT INTO resource_groups(connection_id,kind,name,name_key,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                                  (connection_id, data.kind, data.name, data.name.casefold(), data.notes, now, now)).lastrowid
        db.executemany('INSERT INTO resource_group_members VALUES(?,?,?,?)',
                       [(group_id, connection_id, data.kind, resource) for resource in data.members])
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'A resource group with this name already exists in this connection.') from None
    from . import placement_rules
    placement_rules.validate(db, connection_id)
    return group_for(db, connection_id, group_id)


def delete(db, connection_id, group_id, revision):
    group = group_for(db, connection_id, group_id)
    if group['revision'] != revision:
        raise HTTPException(409, 'This group changed in another session. Refresh before removing it.')
    if db.execute('SELECT 1 FROM placement_rule_groups WHERE group_id=? AND connection_id=?', (group_id, connection_id)).fetchone():
        raise HTTPException(409, 'This group is used by placement rules. Remove those references before deleting it.')
    db.execute('DELETE FROM resource_groups WHERE id=? AND connection_id=?', (group_id, connection_id))
