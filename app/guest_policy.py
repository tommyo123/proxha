import json
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


SCHEMA = '''
CREATE TABLE IF NOT EXISTS guest_policies (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, stickiness INTEGER NOT NULL CHECK(stickiness BETWEEN 1 AND 10),
    revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    PRIMARY KEY(connection_id, resource));
'''


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stickiness: int = Field(ge=1, le=10, strict=True)
    revision: int = Field(ge=0, strict=True)


def preference(stickiness=5, intent='balance', exception_reason=None):
    if type(stickiness) is not int or not 1 <= stickiness <= 10:
        return {'allowed': False, 'code': 'invalid_stickiness', 'reason': 'Stickiness must be an integer from 1 to 10.'}
    if intent not in ('balance', 'maintenance', 'emergency', 'placement_rules'):
        return {'allowed': False, 'code': 'invalid_intent', 'reason': 'Unknown migration purpose.'}
    exceptional = intent != 'balance'
    if exceptional and (not isinstance(exception_reason, str) or not exception_reason.strip()):
        return {'allowed': False, 'code': 'missing_exception_reason', 'reason': 'Maintenance, emergency and rule-repair moves require a verified reason.'}
    protected = stickiness == 10
    # Only evacuation can move a pinned guest; rule repair reports the conflict instead.
    override = protected and intent in ('maintenance', 'emergency')
    return {'allowed': not protected or override, 'stickiness': stickiness,
            'code': 'stickiness_override' if override else 'stickiness_protected' if protected else 'preference_allowed',
            'migration_cost_multiplier': None if protected else round(2 ** ((stickiness - 5) / 4), 4),
            'exception': exception_reason.strip() if exceptional else None,
            'reason': ('Stickiness 10 blocks automatic rule repair. Move this guest manually or lower its stickiness.' if protected and intent == 'placement_rules' else
                       'Stickiness 10 blocks routine automatic balancing.' if protected and not override else
                       'Stickiness protection overridden for verified maintenance or emergency evacuation.' if override else
                       'Lower stickiness reduces migration cost when ranking otherwise eligible moves.')}


def resource_for(connection, kind, vmid):
    resource = f'{kind}/{vmid}'
    snapshot = json.loads(connection['snapshot'] or '{}')
    if not any(g.get('id') == resource for g in snapshot.get('guests', [])):
        raise HTTPException(404, 'Guest not found in this connection. Refresh the cluster inventory.')
    return resource


def read(db, connection_id, resource):
    row = db.execute('SELECT stickiness,revision,updated_at FROM guest_policies WHERE connection_id=? AND resource=?',
                     (connection_id, resource)).fetchone()
    value = dict(row) if row else {'stickiness': 5, 'revision': 0, 'updated_at': None}
    return {**value, 'resource': resource, 'default': 5, 'automatic_execution_enabled': False,
            'routine_migration': preference(value['stickiness'])}


def save(db, connection_id, resource, data):
    current = read(db, connection_id, resource)
    if current['revision'] != data.revision:
        raise HTTPException(409, 'Stickiness changed in another session. Reload the saved value before editing.')
    db.execute('''INSERT INTO guest_policies(connection_id,resource,stickiness,revision,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(connection_id,resource) DO UPDATE SET stickiness=excluded.stickiness,
        revision=excluded.revision,updated_at=excluded.updated_at''',
               (connection_id, resource, data.stickiness, data.revision + 1, datetime.now(timezone.utc).isoformat()))
    return read(db, connection_id, resource)
