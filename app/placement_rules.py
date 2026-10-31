import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from . import migration_groups, node_maintenance, resource_groups
from .proxmox import CandidateRejected, ProxmoxError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS placement_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name TEXT NOT NULL, name_key TEXT NOT NULL, kind TEXT NOT NULL,
    enforcement TEXT NOT NULL DEFAULT 'should', weight REAL NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(connection_id,name_key), UNIQUE(id,connection_id)
);
CREATE TABLE IF NOT EXISTS placement_rule_groups (
    rule_id INTEGER NOT NULL, connection_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
    kind TEXT NOT NULL, PRIMARY KEY(rule_id,group_id),
    FOREIGN KEY(rule_id,connection_id) REFERENCES placement_rules(id,connection_id) ON DELETE CASCADE,
    FOREIGN KEY(group_id,connection_id,kind) REFERENCES resource_groups(id,connection_id,kind) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS placement_intents (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    unit TEXT NOT NULL, policy_key TEXT NOT NULL, target TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(connection_id,unit)
);
CREATE TABLE IF NOT EXISTS placement_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES migration_runs(id) ON DELETE CASCADE,
    document TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL,
    status TEXT NOT NULL DEFAULT 'active', message TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS placement_batch_active ON placement_batches(run_id) WHERE finished_at IS NULL;
CREATE TABLE IF NOT EXISTS placement_failures (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    unit TEXT NOT NULL, target TEXT NOT NULL, attempts INTEGER NOT NULL,
    failed_at REAL NOT NULL, message TEXT NOT NULL,
    PRIMARY KEY(connection_id,unit,target)
);
'''
RETRY_LIMIT = 3
FAILURE_MEMORY = 86400


def upgrade(db):
    if 'enforcement' not in {r['name'] for r in db.execute('PRAGMA table_info(placement_rules)')}:
        db.execute("ALTER TABLE placement_rules ADD COLUMN enforcement TEXT NOT NULL DEFAULT 'should'")
    if 'weight' not in {r['name'] for r in db.execute('PRAGMA table_info(placement_rules)')}:
        db.execute('ALTER TABLE placement_rules ADD COLUMN weight REAL NOT NULL DEFAULT 5')


class RuleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['together', 'apart', 'require_hosts', 'avoid_hosts']
    enforcement: Literal['must', 'should', 'report'] = 'should'
    # Only Should rules use a weight. Without one, a saved rule keeps its stored weight.
    weight: float | None = None
    enabled: bool = Field(default=True, strict=True)
    guest_group_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    host_group_ids: list[StrictInt] = Field(default_factory=list, max_length=100)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        return resource_groups.GroupInput.valid_name(resource_groups.GroupInput.clean_text(value))

    @model_validator(mode='after')
    def valid_groups(self):
        for ids in (self.guest_group_ids, self.host_group_ids):
            if len(ids) != len(set(ids)) or any(i <= 0 for i in ids):
                raise ValueError('Select each group only once.')
        if bool(self.host_group_ids) != (self.kind in ('require_hosts', 'avoid_hosts')):
            raise ValueError('Host placement rules require host groups; together/apart rules use guest groups only.')
        valid = self.weight is not None and 0.1 <= self.weight <= 100
        if self.enforcement == 'should' and self.weight is not None and not valid:
            raise ValueError('Enter a Should weight from 0.1 to 100 score points.')
        if not valid:
            self.weight = None
        return self


class RuleUpdate(RuleInput):
    revision: int = Field(ge=1, strict=True)


class PreviewInput(RuleInput):
    rule_id: StrictInt | None = None
    revision: int | None = Field(default=None, ge=1, strict=True)


def repair_summary(result):
    moves = [{'name': m['name'], 'members': m.get('unit_members') or [m['resource']], 'source': m['source'], 'target': m['target']}
             for m in result['plan'] if m.get('rule_repair')]
    blocked = [{'name': c['name'], 'members': c.get('unit_members') or [c['resource']], 'reasons': c['reasons']}
               for c in result['candidates'] if c.get('evaluation_track') == 'placement_rules' and c.get('status') == 'blocked']
    return moves, blocked, result['message']


def list_rules(db, connection_id):
    groups = {g['id']: g for g in resource_groups.list_groups(db, connection_id)}
    links = {}
    for row in db.execute('SELECT rule_id,group_id FROM placement_rule_groups WHERE connection_id=?', (connection_id,)):
        links.setdefault(row['rule_id'], []).append(groups[row['group_id']])
    rules = []
    for row in db.execute('SELECT * FROM placement_rules WHERE connection_id=? ORDER BY name_key,id', (connection_id,)):
        rule = dict(row)
        rule['enabled'] = bool(rule['enabled'])
        members = links.get(rule['id'], [])
        for kind in ('guest', 'host'):
            rule[kind + '_group_ids'] = sorted(g['id'] for g in members if g['kind'] == kind)
        rule['guests'] = sorted({r for g in members if g['kind'] == 'guest' for r in g['members']})
        rule['hosts'] = sorted({r.split('/', 1)[1] for g in members if g['kind'] == 'host' for r in g['members']})
        rules.append(rule)
    return rules


def find(db, connection_id, rule_id):
    rule = next((r for r in list_rules(db, connection_id) if r['id'] == rule_id), None)
    if not rule:
        raise HTTPException(404, 'Placement rule not found.')
    return rule


def components(rules, resources):
    parent = {r: r for r in resources}

    def root(r):
        parent.setdefault(r, r)
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    for rule in rules:
        members = [r for r in rule['guests'] if r in resources]
        if rule['kind'] == 'together' and members:
            for resource in members[1:]:
                a, b = sorted((root(members[0]), root(resource)))
                parent[b] = a
    units = {}
    for resource in sorted(resources):
        units.setdefault(root(resource), []).append(resource)
    return list(units.values())


def enforced(rules):
    return [r for r in rules if r['enabled'] and r['enforcement'] == 'must']


def validate(db, connection_id):
    all_rules = list_rules(db, connection_id)
    for rule in all_rules:
        if rule['enabled'] and (not rule['guests'] or rule['kind'].endswith('_hosts') and not rule['hosts']):
            raise HTTPException(422, 'Enabled placement rules require nonempty resource groups.')
    rules = enforced(all_rules)
    resources = {g for r in rules for g in r['guests']}
    units = components(rules, resources)
    for rule in rules:
        if rule['kind'] == 'apart' and any(len(set(unit) & set(rule['guests'])) > 1 for unit in units):
            raise HTTPException(422, 'Conflicting rules: the same guests cannot be kept both together and apart, including overlapping groups.')
    for unit in units:
        required, avoided = None, set()
        for rule in rules:
            if not set(unit).intersection(rule['guests']):
                continue
            if rule['kind'] == 'require_hosts':
                required = set(rule['hosts']) if required is None else required & set(rule['hosts'])
            if rule['kind'] == 'avoid_hosts':
                avoided.update(rule['hosts'])
        if required is not None and not required - avoided:
            raise HTTPException(422, 'Conflicting host rules leave a guest or together-group with no configured host.')


def save(db, connection_id, data, rule_id=None):
    previous = find(db, connection_id, rule_id) if rule_id is not None else None
    if previous and previous['revision'] != data.revision:
        raise HTTPException(409, 'This rule changed in another session. Refresh and reopen it.')
    groups = {g['id']: g for g in resource_groups.list_groups(db, connection_id)}
    for kind, ids in (('guest', data.guest_group_ids), ('host', data.host_group_ids)):
        if any(i not in groups or groups[i]['kind'] != kind for i in ids):
            raise HTTPException(422, 'Select groups of the correct type from this cluster.')
    now = datetime.now(timezone.utc).isoformat()
    # Must and Report-only rules keep the stored Should weight for a later switch back.
    weight = data.weight if data.weight is not None else previous['weight'] if previous else 5
    try:
        if previous:
            db.execute('UPDATE placement_rules SET name=?,name_key=?,kind=?,enforcement=?,weight=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                       (data.name, data.name.casefold(), data.kind, data.enforcement, weight, data.enabled, now, rule_id))
            db.execute('DELETE FROM placement_rule_groups WHERE rule_id=?', (rule_id,))
        else:
            rule_id = db.execute('INSERT INTO placement_rules(connection_id,name,name_key,kind,enforcement,weight,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                 (connection_id, data.name, data.name.casefold(), data.kind, data.enforcement, weight, data.enabled, now, now)).lastrowid
        db.executemany('INSERT INTO placement_rule_groups VALUES(?,?,?,?)',
                       [(rule_id, connection_id, i, kind) for kind, ids in (('guest', data.guest_group_ids), ('host', data.host_group_ids)) for i in ids])
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'A placement rule with this name already exists.') from None
    validate(db, connection_id)
    return find(db, connection_id, rule_id)


def delete(db, connection_id, rule_id, revision):
    if find(db, connection_id, rule_id)['revision'] != revision:
        raise HTTPException(409, 'This rule changed in another session. Refresh before removing it.')
    db.execute('DELETE FROM placement_rules WHERE id=? AND connection_id=?', (rule_id, connection_id))


def retry_delay(attempts):
    return min(900, 60 * 2 ** (attempts - 1))


def record_failure(db, connection_id, unit, target, message, now):
    row = db.execute('SELECT attempts FROM placement_failures WHERE connection_id=? AND unit=? AND target=?',
                     (connection_id, unit, target)).fetchone()
    attempts = (row['attempts'] if row else 0) + 1
    db.execute('INSERT OR REPLACE INTO placement_failures VALUES(?,?,?,?,?,?)', (connection_id, unit, target, attempts, now, message))
    if attempts >= RETRY_LIMIT:
        # Release the fixed repair target so another compatible host can be tried.
        db.execute('DELETE FROM placement_intents WHERE connection_id=? AND unit=? AND target=?', (connection_id, unit, target))
    return attempts


def failures(db, connection_id, now):
    rows = db.execute('SELECT * FROM placement_failures WHERE connection_id=? AND failed_at>=?', (connection_id, now - FAILURE_MEMORY))
    return {(r['unit'], r['target']): {**dict(r), 'retry_at': r['failed_at'] + retry_delay(r['attempts'])} for r in rows}


def clear_failures(db, connection_id, unit):
    db.execute('DELETE FROM placement_failures WHERE connection_id=? AND unit=?', (connection_id, unit))


def prune(db, now):
    db.execute('DELETE FROM placement_failures WHERE failed_at<?', (now - FAILURE_MEMORY,))


class Policy:
    def __init__(self, db, connection_id, resources, excluded=(), native=None):
        from . import ha_guests
        rules = list_rules(db, connection_id)
        self.native = ha_guests.cached(db, connection_id) if native is None else native
        self.excluded = set(excluded) | node_maintenance.excluded(db, connection_id) | ha_guests.maintenance_hosts(self.native)
        eligible_hosts = [r for r in resources if r.get('type') != 'node' or r['node'] not in self.excluded]
        self.rules = enforced(rules) + ha_guests.placement_constraints(self.native, eligible_hosts)
        self.preferences = [r for r in rules if r['enabled'] and r['enforcement'] == 'should']
        # Report-only rules are measured but never change plans or the policy key.
        self.reported = [r for r in rules if r['enabled'] and r['enforcement'] == 'report']
        entries = sorted(({k: r.get(k, 'started' if k == 'state' else 1 if k == 'failback' else None)
                           for k in ('sid', 'state', 'group', 'failback')} for r in self.native.get('ha_resources') or []), key=lambda r: r['sid'])
        self.key = hashlib.sha256(json.dumps({'rules': self.rules, 'preferences': self.preferences, 'ha_resources': entries}, sort_keys=True).encode()).hexdigest()[:16]
        self.nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
        self.available = {n for n, r in self.nodes.items() if r.get('status') == 'online' and n not in self.excluded}
        self.guests = {r['id']: r for r in resources if r.get('type') in ('qemu', 'lxc') and r.get('status') == 'running'}
        self.placements = {r: g['node'] for r, g in self.guests.items()}
        self.units = components(self.rules, {r for r, n in self.placements.items() if n in self.available})
        self.by_guest = {r: unit for unit in self.units for r in unit}
        self.by_guest.update({r: [r] for r in self.guests if r not in self.by_guest})
        self.migration_groups = migration_groups.list_groups(db, connection_id)

    def domain(self, unit, strict=False):
        hosts = self.available.copy()
        for resource in unit:
            source = self.placements[resource]
            groups = [g for g in self.migration_groups if g['enabled'] and source in g['nodes']]
            hosts &= {n for g in groups for n in g['nodes']} or {source}
        constrained = hosts.copy()
        for rule in self.rules:
            if not set(unit).intersection(rule['guests']):
                continue
            if rule['kind'] == 'require_hosts':
                constrained.intersection_update(rule['hosts'])
            elif rule['kind'] == 'avoid_hosts':
                constrained.difference_update(rule['hosts'])
        # Availability exceptions never cross migration compatibility boundaries.
        return constrained if strict or constrained else hosts

    def count(self, rule, placements):
        members = [r for r in rule['guests'] if r in placements and placements[r] in self.available]
        counts = Counter(placements[r] for r in members)
        if rule['kind'] == 'together':
            return sum(a * b for a, b in combinations(counts.values(), 2))
        if rule['kind'] == 'apart':
            return sum(n * (n - 1) // 2 for n in counts.values())
        return sum((placements[r] not in rule['hosts']) if rule['kind'] == 'require_hosts' else (placements[r] in rule['hosts']) for r in members)

    def violations(self, placements=None):
        placements = placements if placements is not None else self.placements
        return sum(self.count(rule, placements) for rule in self.rules)

    def preference_cost(self, placements=None):
        placements = self.placements if placements is None else placements
        return sum(self.rule_preference_cost(rule, placements) for rule in self.preferences)

    def rule_preference_cost(self, rule, placements):
        count = len([r for r in rule['guests'] if r in placements and placements[r] in self.available])
        scale = max(1, count * (count - 1) / 2 if rule['kind'] in ('apart', 'together') else count)
        return rule['weight'] * self.count(rule, placements) / scale

    def preference_gain(self, changes, placements=None):
        before = self.placements if placements is None else placements
        return self.preference_cost(before) - self.preference_cost({**before, **changes})

    def check(self, changes, placements=None, maintenance=False):
        before = self.placements if placements is None else placements
        after = {**before, **changes}
        overrides = []
        if self.rules and any(target not in self.available for target in changes.values()):
            return False, 'Placement target is offline or in maintenance.', overrides
        for rule in self.rules:
            selected = set(changes).intersection(rule['guests'])
            if not selected:
                continue
            new = self.count(rule, after)
            if rule['kind'].endswith('_hosts'):
                violated = [r for r in selected if r in self.guests and
                            ((after[r] not in rule['hosts']) if rule['kind'] == 'require_hosts' else (after[r] in rule['hosts']))]
                unavailable = all(after[r] in self.domain(self.by_guest[r]) and not self.domain(self.by_guest[r], strict=True) for r in violated)
                invalid = bool(violated)
                if invalid and unavailable and not rule.get('native'):
                    overrides.append(f'{rule["name"]}: no permitted host is available in the migration domain.')
                    continue
            else:
                invalid = new > 0 and (rule['kind'] == 'together' or any(
                    after.get(peer) == after[r] for r in selected for peer in rule['guests'] if peer != r))
                if rule['kind'] == 'apart' and new:
                    # When every eligible host already has peers, choose minimum collisions.
                    unavoidable = all(
                        sum(after.get(peer) == after[r] for peer in rule['guests'] if peer != r) ==
                        min((sum(after.get(peer) == target for peer in rule['guests'] if peer != r)
                             for target in self.domain(self.by_guest[r])), default=0) > 0 for r in selected)
                    if unavoidable and not rule.get('native'):
                        overrides.append(f'{rule["name"]}: too few available hosts; using the least conflicting placement.')
                        continue
            if invalid:
                if maintenance and not rule.get('native'):
                    overrides.append(f'{rule["name"]}: maintenance evacuation takes priority.')
                else:
                    return False, f'Placement rule "{rule["name"]}" would be violated.', overrides
        return True, None, overrides

    def details(self, rule):
        placements = self.placements
        members = [r for r in rule['guests'] if r in placements and placements[r] in self.available]
        counts = Counter(placements[r] for r in members)
        if rule['kind'] == 'together':
            home = min(counts, key=lambda n: (-counts[n], n), default=None)
            found = [(r, f'Runs on {placements[r]}; most members run on {home}.') for r in members if placements[r] != home]
        elif rule['kind'] == 'apart':
            found = [(r, f'Shares {placements[r]} with ' + ', '.join(p for p in members if p != r and placements[p] == placements[r]) + '.')
                     for r in members if counts[placements[r]] > 1]
        elif rule['kind'] == 'require_hosts':
            found = [(r, f'Runs on {placements[r]}, which is not a permitted host.') for r in members if placements[r] not in rule['hosts']]
        else:
            found = [(r, f'Runs on avoided host {placements[r]}.') for r in members if placements[r] in rule['hosts']]
        return [{'resource': r, 'name': self.guests[r].get('name') or r, 'node': placements[r], 'reason': reason} for r, reason in found]

    def statuses(self):
        result = []
        for rule in self.rules + self.preferences + self.reported:
            missing = [r for r in rule['guests'] if r not in self.guests]
            unavailable = [r for r in rule['guests'] if r in self.placements and self.placements[r] not in self.available]
            violations = self.count(rule, self.placements)
            active = [r for r in rule['guests'] if r in self.by_guest and self.placements[r] in self.available]
            scarce = rule['kind'] == 'apart' and any(len(self.domain(self.by_guest[r])) < len(active) for r in active)
            homeless = [r for r in active if rule['kind'].endswith('_hosts') and not self.domain(self.by_guest[r], strict=True)]
            relaxed = rule['enforcement'] == 'should' or scarce or homeless
            warnings = [f'{r}: no permitted host is available in its migration domain.' for r in homeless]
            if scarce:
                warnings.append('Fewer available hosts than members in the migration domain.')
            if rule['enforcement'] == 'should':
                warnings.append('Weighted preference: considered alongside balancing benefit and migration cost; no immediate repair is forced.')
            result.append({'id': rule['id'], 'name': rule['name'], 'violations': violations, 'enforcement': rule['enforcement'],
                           'weight': rule.get('weight', 0), 'preference_cost': self.rule_preference_cost(rule, self.placements) if rule['enforcement'] == 'should' else 0,
                           'state': 'degraded' if unavailable or violations and relaxed else 'violated' if violations else 'satisfied',
                           'inactive_members': missing, 'unavailable_members': unavailable,
                           'violating_members': self.details(rule), 'warnings': warnings})
        return result


def ha_sid(resource):
    kind, vmid = resource.split('/', 1)
    return ('vm:' if kind == 'qemu' else 'ct:') + vmid


def ha_list(value):
    return [v.strip() for v in (value if isinstance(value, list) else str(value or '').split(',')) if v.strip()]


# Native HA owns the placement of managed guests and may enforce its own affinity rules.
def ha_conflicts(rules, native):
    managed = {r.get('sid') for r in native.get('ha_resources') or [] if isinstance(r, dict)}
    result = {}
    for rule in rules:
        members = {ha_sid(r): r for r in rule['guests']}
        owned = sorted(r for s, r in members.items() if s in managed)
        notes = [f'Native HA manages {", ".join(owned[:5])}{" and others" if len(owned) > 5 else ""}; HA balancing must be enabled and native constraints still apply.'] if owned else []
        for ha in native.get('ha_rules') or []:
            if not isinstance(ha, dict) or ha.get('disable'):
                continue
            shared = sorted(members[s] for s in set(ha_list(ha.get('resources'))) & members.keys())
            name = ha.get('rule') or 'unnamed'
            if ha.get('type') == 'resource-affinity' and len(shared) > 1:
                if rule['kind'] == 'together' and ha.get('affinity') == 'negative' or rule['kind'] == 'apart' and ha.get('affinity') == 'positive':
                    notes.append(f'Conflicts with native HA {ha.get("affinity")} affinity rule "{name}" for {", ".join(shared)}.')
            if ha.get('type') == 'node-affinity' and shared and rule['kind'].endswith('_hosts'):
                nodes = {n.split(':')[0] for n in ha_list(ha.get('nodes'))}
                if ha.get('affinity') == 'negative':
                    if rule['kind'] == 'require_hosts' and nodes and not set(rule['hosts']) - nodes:
                        notes.append(f'Native HA node affinity rule "{name}" {"excludes" if ha.get("strict") else "prefers avoiding"} every required host for {", ".join(shared)}.')
                    continue
                allowed = nodes & set(rule['hosts']) if rule['kind'] == 'require_hosts' else nodes - set(rule['hosts'])
                if nodes and not allowed:
                    notes.append(f'Native HA node affinity rule "{name}" {"requires" if ha.get("strict") else "prefers"} hosts this rule does not permit for {", ".join(shared)}.')
        result[rule['id']] = notes
    return result


async def live_check(client, db, connection_id, changes, maintenance=False, placements=None, starting=False):
    from . import ha_guests
    native = ha_guests.cached(db, connection_id)
    if not native.get('ha_resources') and not db.execute('SELECT 1 FROM placement_rules WHERE connection_id=? AND enabled=1', (connection_id,)).fetchone():
        return Policy(db, connection_id, []), []
    resources = await client.get('cluster/resources')
    if starting:
        if set(changes) - {r.get('id') for r in resources if r.get('type') in ('qemu', 'lxc')}:
            raise CandidateRejected('A selected start guest is no longer visible.')
        resources = [{**r, 'status': 'running', 'node': changes[r['id']]} if r.get('id') in changes else r for r in resources]
    ha = await client.get('cluster/ha/status/current')
    if native.get('ha_resources'):
        native = {'ha_resources': await client.get('cluster/ha/resources'), 'ha_rules': await client.get('cluster/ha/rules')}
    excluded = {r['node'] for r in ha if r.get('node') and node_maintenance.native_maintenance(r)}
    policy = Policy(db, connection_id, resources, excluded, native=native)
    allowed, reason, overrides = policy.check(changes, placements, maintenance)
    if not allowed:
        raise CandidateRejected(reason)
    return policy, overrides
