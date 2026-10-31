import json
import re
from .storage import flag

# Proxmox status timestamps come from cluster clocks; tolerate skew ahead of ProxHA.
CLOCK_SKEW = 60


def recent(timestamp, now, limit=60):
    return isinstance(timestamp, (int, float)) and -CLOCK_SKEW <= now - timestamp <= limit


def sid(resource):
    from . import placement_rules
    return placement_rules.ha_sid(resource)


def resource(key):
    if re.fullmatch(r'(vm|ct):[0-9]+', str(key)):
        return ('qemu/' if key.startswith('vm:') else 'lxc/') + key[3:]


def members(value):
    return [str(v).strip() for v in (value if isinstance(value, list) else str(value or '').split(',')) if str(v).strip()]


def stable(status, key, node, now):
    master = next((r for r in status if r.get('type') == 'master'), {})
    service = next((r for r in status if r.get('sid') == key), {})
    return (recent(master.get('timestamp'), now)
            and service.get('crm_state') == 'started' and service.get('node') == node
            and not any(r.get('armed-state') in ('disarmed', 'disarming') for r in status))


def cached(db, connection_id):
    row = db.execute('SELECT native_policy FROM activity_status WHERE connection_id=?', (connection_id,)).fetchone()
    return json.loads(row['native_policy'] or '{}') if row else {}


def handoff_task(db, connection_id, run_id, task):
    """Whether a native migration task is the LRM side of an HA move this run requested."""
    if not run_id or not task['resource'] or not isinstance(task['started_at'], (int, float)):
        return False
    started = task['started_at'] + CLOCK_SKEW
    for step in db.execute('''SELECT s.source,s.created_at,o.payload FROM migration_run_steps s
            JOIN operations o ON o.id=s.operation_id WHERE s.run_id=? AND s.resource=?''', (run_id, task['resource'])):
        if step['source'] == task['node'] and started >= step['created_at'] and json.loads(step['payload']).get('ha_managed'):
            return True
    # Native maintenance evacuates HA resources with LRM tasks on the maintenance host.
    request = db.execute('''SELECT m.requested_at FROM migration_runs r JOIN native_ha_maintenance m
        ON m.connection_id=r.connection_id AND m.node=r.maintenance_node
        WHERE r.id=? AND m.desired=1 AND m.node=?''', (run_id, task['node'])).fetchone()
    if not request or started < request['requested_at']:
        return False
    managed = cached(db, connection_id).get('ha_resources') or []
    return task['resource'] in {resource(r.get('sid')) for r in managed if isinstance(r, dict)}


def maintenance_hosts(native):
    from . import node_maintenance
    return {r['node'] for r in native.get('ha_status', []) if r.get('node') and node_maintenance.native_maintenance(r)}


def eligible(entry, rules, key):
    if not entry or entry.get('state', 'started') != 'started' or entry.get('group'):
        return False
    if not isinstance(rules, list):
        return False
    return all(valid_rule(r) for r in rules if flag(r.get('disable', 0)) is not True and key in members(r.get('resources')))


def valid_rule(rule):
    if not rule.get('rule'):
        return False
    if rule.get('type') == 'resource-affinity':
        return rule.get('affinity') in ('positive', 'negative')
    if rule.get('type') == 'node-affinity':
        affinity = rule.get('affinity', 'positive')
        nodes = members(rule.get('nodes'))
        return (affinity in ('positive', 'negative') and bool(nodes)
                and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?', n) for n in nodes)
                and (affinity != 'negative' or all(':' not in n for n in nodes)))
    return False


def movable(db, connection_id):
    native = cached(db, connection_id)
    if not isinstance(native.get('ha_rules'), list):
        return set()
    entries = {r.get('sid'): r for r in native.get('ha_resources') or [] if isinstance(r, dict)}
    return {('qemu/' if key.startswith('vm:') else 'lxc/') + key[3:] for key, entry in entries.items()
            if key and eligible(entry, native['ha_rules'], key)}


def placement_constraints(native, resources, select=False):
    """Native HA placement limits; select models the CRM choosing a node rather than keeping one."""
    available = {r['node'] for r in resources if r.get('type') == 'node' and r.get('status') == 'online'} - maintenance_hosts(native)
    entries = {r['sid']: r for r in native.get('ha_resources') or []}
    result = []
    for rule in native.get('ha_rules') or []:
        if flag(rule.get('disable', 0)) is True or not valid_rule(rule):
            continue
        keys = [k for k in members(rule.get('resources')) if resource(k) and k in entries]
        base = {'id': 'native:' + rule['rule'], 'name': 'Native HA: ' + rule['rule'], 'native': True,
                'enabled': True, 'enforcement': 'must', 'guests': [resource(k) for k in keys], 'hosts': []}
        if rule.get('type') == 'resource-affinity':
            if rule.get('affinity') in ('positive', 'negative'):
                result.append({**base, 'kind': 'together' if rule['affinity'] == 'positive' else 'apart'})
        elif rule.get('type') == 'node-affinity':
            if rule.get('affinity') == 'negative':
                allowed = available - set(members(rule['nodes']))
                if not allowed and flag(rule.get('strict', 0)) is False:
                    allowed = available
                result.append({**base, 'kind': 'require_hosts', 'hosts': sorted(allowed)})
                continue
            weights = {}
            for value in members(rule.get('nodes')):
                node, _, weight = value.partition(':')
                weights[node] = int(weight or 0)
            online = available.intersection(weights)
            relaxed = flag(rule.get('strict', 0)) is False
            for key in keys:
                if not online:
                    hosts = available if relaxed else set(weights)
                elif flag(entries[key].get('failback', 1)) is not False:
                    highest = max(weights[n] for n in online)
                    hosts = {n for n in online if weights[n] == highest}
                else:
                    # Without failback, a non-strict rule keeps the guest on any online node.
                    hosts = online if select or not relaxed else available
                result.append({**base, 'id': base['id'] + ':' + key, 'kind': 'require_hosts',
                               'guests': [resource(key)], 'hosts': sorted(hosts)})
    return result


def cohort(native, key):
    result = {key}
    changed = True
    while changed:
        changed = False
        for rule in native.get('ha_rules') or []:
            peers = set(members(rule.get('resources')))
            if flag(rule.get('disable', 0)) is not True and rule.get('type') == 'resource-affinity' and rule.get('affinity') == 'positive' and peers & result:
                added = peers - result
                result.update(added)
                changed |= bool(added)
    return {resource(k) for k in result if resource(k)}
