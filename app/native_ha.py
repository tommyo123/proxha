import asyncio
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import backup_guard, ha_guests
from .proxmox import ProxmoxError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS native_ha_maintenance (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, owned INTEGER NOT NULL, desired INTEGER NOT NULL,
    requested_at REAL NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
    empty_since REAL, PRIMARY KEY(connection_id,node)
);
'''
# The CRM must record a request within this window; afterwards a missing request was not applied.
ACK_SECONDS = 120
INTERRUPTED = 'Native HA command was interrupted. Check native HA state; no command will be replayed.'
STALE = ('Native HA has not acknowledged the request and its manager status is stale. '
         'Check native HA in Proxmox; ProxHA will not resend until the outcome is known.')
PENDING = ' ProxHA decides from native HA state once the HA manager has run for 2 minutes after the request.'
# HA skips these requested states; failed services need operator action.
UNMANAGED = ('stopped', 'disabled', 'ignored')
# Background checks of ProxHA maintenance hosts that no evacuation run follows.
REVIEW_SECONDS = 30
COVER = ('Native HA maintenance requested so native HA does not place guests on this ProxHA maintenance host. '
         'Waiting for acknowledgement.')
EXTERNAL = 'Waiting for native HA evacuation; maintenance was enabled outside ProxHA.'
EXTERNAL_OFF = ('Native HA maintenance was disabled outside ProxHA. Resume evacuation to request it from ProxHA, '
                'or exit maintenance.')


class Blocked(ProxmoxError):
    """Native HA evacuation needs manual action before it can finish."""

    def __init__(self, message, blockers):
        super().__init__(message)
        self.blockers = blockers


class Waiting(ProxmoxError):
    """Native HA entry waits until other ProxHA maintenance hosts are in native maintenance."""

    def __init__(self, message):
        super().__init__(message, retryable=True)


def configured():
    return bool(os.environ.get('PROXHA_HA_SSH_KEY') and os.environ.get('PROXHA_HA_SSH_KNOWN_HOSTS'))


async def inspect(client):
    resources = await client.get('cluster/ha/resources')
    if not isinstance(resources, list):
        raise ProxmoxError('Native HA resource inventory is unavailable.')
    rules, status, manager = await asyncio.gather(*(client.get(path) for path in
        ('cluster/ha/rules', 'cluster/ha/status/current', 'cluster/ha/status/manager_status')))
    if not isinstance(rules, list) or not isinstance(status, list) or not isinstance(manager, dict):
        raise ProxmoxError('Native HA policy or manager status is unavailable.')
    return {'ha_resources': resources, 'ha_rules': rules, 'ha_status': status, 'ha_manager': manager}


def summary(native):
    resources, rules = native.get('ha_resources'), native.get('ha_rules')
    fencing = next((r for r in native.get('ha_status', []) if r.get('type') == 'fencing'), {})
    return {'state': 'unavailable' if not isinstance(resources, list) else 'unused' if not resources else fencing.get('armed-state', 'configured'),
            'resource_count': len(resources) if isinstance(resources, list) else None,
            'rule_count': len(rules) if isinstance(rules, list) else None,
            'ssh_configured': configured()}


def read(db, connection_id, node):
    row = db.execute('SELECT * FROM native_ha_maintenance WHERE connection_id=? AND node=?', (connection_id, node)).fetchone()
    return dict(row) if row else None


def host_state(native, node, now):
    data = native.get('ha_manager', {})
    manager, lrm = data.get('manager_status', {}), data.get('lrm_status', {}).get(node, {})
    request = manager.get('node_request', {}).get(node, {}).get('maintenance') in (1, True, '1')
    timestamp = lrm.get('timestamp') if isinstance(lrm.get('timestamp'), (int, float)) else 0
    fresh = ha_guests.recent(timestamp, now)
    services = manager.get('service_status', {})
    active = [sid for sid, s in services.items() if s.get('node') == node and s.get('state') not in ('stopped', 'disabled')]
    # An idle LRM stays in wait_for_agent_lock during maintenance.
    waiting = fresh and lrm.get('state') == 'wait_for_agent_lock' and not active
    crm = manager.get('timestamp')
    return {'maintenance': request or lrm.get('mode') == 'maintenance', 'request': request,
            'confirmed': request and fresh and (lrm.get('mode') == 'maintenance' or waiting),
            'idle': waiting or fresh and lrm.get('state') == 'maintenance' and not active,
            'active_services': active, 'timestamp': timestamp,
            'manager_timestamp': crm if ha_guests.recent(crm, now) else None,
            'normal': fresh and not request and lrm.get('mode') == 'active'}


def covered(native, now):
    """Hosts whose native maintenance keeps the CRM from selecting them."""
    lrms = native.get('ha_manager', {}).get('lrm_status', {})
    return ha_guests.maintenance_hosts(native) | {n for n in lrms if host_state(native, n, now)['confirmed']}


def settle(db, connection_id, node, row, status, now, pending=True):
    """Resolve an unconfirmed command from the CRM request and LRM mode."""
    if not row or row['state'] not in ('submitting', 'unknown', 'exiting', 'evacuating', 'ready'):
        return row
    state, message = row['state'], row['message']
    elapsed = now - row['requested_at']
    # A missing request proves nothing until the CRM has run for the whole window after the request.
    settled = (elapsed >= ACK_SECONDS and status['manager_timestamp'] is not None
               and status['manager_timestamp'] - row['requested_at'] >= ACK_SECONDS)
    if not row['owned']:
        if state == 'unknown':
            state, message = 'submitting', EXTERNAL
    elif row['desired']:
        if status['maintenance']:
            if state == 'unknown':
                state, message = 'submitting', 'Native HA recorded the maintenance request. Waiting for acknowledgement.'
        elif state == 'unknown' or pending and state == 'submitting' and elapsed >= ACK_SECONDS:
            if settled and state == 'unknown':
                state, message = 'failed', 'The earlier native HA maintenance request was not applied. Resume evacuation to request it again, or exit maintenance.'
            elif settled:
                state, message = 'failed', ('Native HA did not acknowledge the maintenance request within 2 minutes. '
                                            'Check the HA manager in Proxmox, then resume evacuation to retry or exit maintenance.')
            elif state == 'submitting':
                state, message = 'unknown', STALE
        elif pending and state in ('evacuating', 'ready') and status['normal'] and status['timestamp'] > row['requested_at']:
            state, message = 'failed', 'Native HA maintenance was disabled outside ProxHA. Resume evacuation to request it again, or exit maintenance.'
    elif status['normal'] and status['timestamp'] > row['requested_at']:
        if state == 'unknown':
            state, message = 'exiting', 'Native HA maintenance has ended.'
    elif status['request'] and (state == 'unknown' or state == 'exiting' and elapsed >= ACK_SECONDS):
        if settled and state == 'unknown':
            state, message = 'failed', 'The earlier native HA exit request was not applied. Exit maintenance again to retry.'
        elif settled:
            state, message = 'failed', 'Native HA did not acknowledge the exit request within 2 minutes. Check the HA manager in Proxmox, then exit maintenance again.'
        elif state == 'exiting':
            state, message = 'unknown', STALE
    if (state, message) == (row['state'], row['message']):
        return row
    db.execute('UPDATE native_ha_maintenance SET state=?,message=? WHERE connection_id=? AND node=?', (state, message, connection_id, node))
    if not row['desired'] and state in ('failed', 'unknown'):
        db.execute("UPDATE node_maintenance SET state='blocked',message=?,updated_at=? WHERE connection_id=? AND node=? AND enabled=1 AND state='exiting'",
                   (message, now, connection_id, node))
    return {**row, 'state': state, 'message': message}


async def command(client, node, enable):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node):
        raise ProxmoxError('Invalid native HA host name.')
    if not configured():
        raise ProxmoxError('Configure native HA SSH or run ha-manager crm-command node-maintenance '
                           + ('enable ' if enable else 'disable ') + node + ' on a Proxmox host.')
    key = Path(os.environ['PROXHA_HA_SSH_KEY']).resolve()
    known = Path(os.environ['PROXHA_HA_SSH_KNOWN_HOSTS']).resolve()
    if not key.is_file() or not known.is_file():
        raise ProxmoxError('Native HA SSH key or known_hosts file is unavailable.')
    user = os.environ.get('PROXHA_HA_SSH_USER', 'root')
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', user):
        raise ProxmoxError('Invalid native HA SSH user.')
    host = urlsplit(client.pool.snapshot()['active'] or '').hostname
    if not host or not re.fullmatch(r'[A-Za-z0-9:.%-]+', host) or host.startswith('-'):
        raise ProxmoxError('Invalid native HA SSH endpoint.')
    known = known.as_posix()
    if '"' in known:
        raise ProxmoxError('Invalid native HA known_hosts path.')
    # Some OpenSSH builds reject a quoted value without whitespace, so quote only when needed.
    known = f'"{known}"' if any(c.isspace() for c in known) else known
    args = ['ssh', '-F', 'none', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
            '-i', str(key), '-l', user, host,
            '/usr/sbin/ha-manager crm-command node-maintenance ' + ('enable ' if enable else 'disable ') + node]
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError:
        raise ProxmoxError('Could not start SSH for native HA maintenance.') from None
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=25)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        # Shutdown must not wait for a command whose outcome is checked from native state later.
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ProxmoxError('Native HA command outcome is unknown. Check native HA status before retrying.', uncertain=True) from None
    if process.returncode:
        lines = [l.strip() for l in (error or b'').decode(errors='replace').splitlines() if l.strip()]
        detail = ' SSH reported: ' + ''.join(c for c in lines[-1] if c.isprintable())[:200] if lines else ''
        raise ProxmoxError('Native HA SSH command failed; its outcome must be checked in Proxmox. Check SSH access and host keys.' + detail, uncertain=True)


def unresolved(row):
    return row['message'] if row['message'] == STALE or row['state'] != 'unknown' else row['message'] + PENDING


async def send(db, connection_id, node, client, enable):
    try:
        await command(client, node, enable)
    except ProxmoxError as exc:
        db.execute('UPDATE native_ha_maintenance SET state=?,message=? WHERE connection_id=? AND node=?',
                   ('unknown' if exc.uncertain else 'failed', str(exc), connection_id, node))
        db.commit()
        raise
    except asyncio.CancelledError:
        db.execute("UPDATE native_ha_maintenance SET state='unknown',message=? WHERE connection_id=? AND node=?", (INTERRUPTED, connection_id, node))
        db.commit()
        raise


async def validate_entry(client, db, connection_id, node, native, resources, restart_containers, now):
    """Check native evacuation from node; returns ProxHA-only maintenance hosts native HA could still select."""
    from . import migration_groups, migration_queue, node_maintenance, placement_rules, storage
    await node_maintenance.verify_inventory_access(client)
    await backup_guard.require_clear(client, resources)
    tasks = await client.get('cluster/tasks')
    if any(t.get('type') in ('qmigrate', 'vzmigrate') and not t.get('endtime') and t.get('status') in (None, 'running', 'unknown') for t in tasks):
        raise ProxmoxError('Wait for active native migrations before entering native HA maintenance.')
    quorum = await client.get('cluster/status')
    if not any(r.get('type') == 'cluster' and r.get('quorate') in (1, True) for r in quorum):
        raise ProxmoxError('Cluster quorum could not be confirmed.')
    if any(r.get('armed-state') in ('disarmed', 'disarming') for r in native['ha_status']):
        raise ProxmoxError('Native HA is disarmed. Resolve its state in Proxmox before maintenance.')
    native_hosts = covered(native, now) | {node}
    excluded = node_maintenance.excluded(db, connection_id) | native_hosts
    # Native HA ignores ProxHA exclusions, so it may choose any online host outside native maintenance.
    reserved = excluded - native_hosts
    pending = set()
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    available = {n for n, r in nodes.items() if r.get('status') == 'online' and n not in native_hosts}
    inventory = {r['id']: r for r in resources if r.get('type') in ('qemu', 'lxc')}
    entries = {r['sid']: r for r in native['ha_resources']}
    constraints = ha_guests.placement_constraints(native, [r for r in resources if r.get('type') != 'node' or r['node'] in available], select=True)
    groups = migration_groups.list_groups(db, connection_id)
    policy = placement_rules.Policy(db, connection_id, resources, excluded=excluded, native=native)
    for guest in node_maintenance.residents(resources, node):
        key = ha_guests.sid(guest['id'])
        if key not in entries:
            continue
        cohort = ha_guests.cohort(native, key)
        targets = set(available)
        for rule in constraints:
            if not cohort.intersection(rule['guests']):
                continue
            if rule['kind'] == 'require_hosts':
                targets.intersection_update(rule['hosts'])
            elif rule['kind'] == 'apart':
                targets.difference_update(inventory[r]['node'] for r in rule['guests'] if r not in cohort and r in inventory)
        # ProxHA-only maintenance hosts enter native maintenance before this request, so they are no target.
        pending |= targets & reserved
        targets -= reserved
        if not targets:
            raise ProxmoxError(f'{key}: native HA rules leave no evacuation target outside maintenance.')
        for resource in cohort:
            member = inventory.get(resource)
            if not member or member.get('status') != 'running' or not ha_guests.eligible(entries.get(ha_guests.sid(resource)), native['ha_rules'], ha_guests.sid(resource)):
                raise ProxmoxError(f'{resource}: resolve stopped, unavailable or unsupported HA resources in Proxmox before evacuation.')
            if member['type'] == 'lxc' and not restart_containers:
                raise ProxmoxError('Native HA evacuation includes containers. Allow restart migration before continuing.')
            source = member['node']
            config = await client.get(f'nodes/{source}/{member["type"]}/{member["vmid"]}/config?current=1')
            if member.get('lock') or config.get('lock') or config.get('template'):
                raise ProxmoxError(f'{resource}: locked guests and templates cannot evacuate automatically.')
            inventories = {n: {s['storage']: s for s in await client.get(f'nodes/{n}/storage')} for n in targets | {source}}
            disk = {**storage.inspect(config, source, inventories), 'node': source}
            for target in targets - {source}:
                boundary = migration_groups.pair_policy(groups, nodes, source, target)
                reason = storage.block_reason(disk, source, target)
                if not boundary['allowed'] or reason:
                    raise ProxmoxError(f'{resource}: native HA could select {target}: {reason or boundary["reason"]}. Constrain its native node-affinity rule first.')
                check = await client.get(f'nodes/{source}/{member["type"]}/{member["vmid"]}/migrate?target={target}')
                network = await client.get(f'nodes/{target}/network')
                migration_queue.check_devices(member, config, check, inventories, network, target, cohort, restart_containers)
        for target in targets:
            allowed, reason, _ = policy.check(dict.fromkeys(cohort, target), maintenance=True)
            if not allowed:
                raise ProxmoxError(reason)
    return pending


def unmanaged(native, residents):
    """Resident HA guests that native maintenance will never move."""
    entries = {r.get('sid'): r for r in native.get('ha_resources', [])}
    services = native.get('ha_manager', {}).get('manager_status', {}).get('service_status', {})
    result = []
    for guest in residents:
        key = ha_guests.sid(guest['id'])
        state = (entries.get(key) or {}).get('state', 'started')
        if state not in UNMANAGED and services.get(key, {}).get('state') == 'error':
            state = 'failed'
        if state in UNMANAGED or state == 'failed':
            result.append({'resource': guest['id'], 'name': guest.get('name'), 'retryable': False, 'code': 'native_ha_unmanaged',
                           'reason': f'Native HA does not move {state} HA resources during maintenance. '
                                     'Migrate it or change its HA state in Proxmox, then resume evacuation.'})
    return result


def observe(db, connection_id, node, native, resources, now):
    from . import node_maintenance
    row = read(db, connection_id, node)
    status = host_state(native, node, now)
    if not row:
        return status
    if not row['desired'] and status['normal'] and status['timestamp'] > row['requested_at']:
        db.execute('DELETE FROM native_ha_maintenance WHERE connection_id=? AND node=?', (connection_id, node))
        db.execute("UPDATE node_maintenance SET enabled=0,state='normal',message='Maintenance ended. Native HA may return evacuated guests to this host.',updated_at=?,blockers='[]' WHERE connection_id=? AND node=?", (now, connection_id, node))
        return {**status, 'ready': False}
    row = settle(db, connection_id, node, row, status, now, pending=False)
    if not row['desired']:
        return {**status, 'ready': False}
    keys = {ha_guests.resource(r['sid']) for r in native.get('ha_resources', [])}
    residents = [r for r in resources if r.get('id') in keys and r.get('node') == node]
    empty = status['confirmed'] and status['idle'] and not residents
    since = (row['empty_since'] if row['empty_since'] is not None else status['timestamp']) if empty else None
    ready = empty and status['timestamp'] >= since + 10
    message = 'Native HA evacuation confirmed.' if ready else 'Waiting for native HA to evacuate and acknowledge maintenance.' if status['confirmed'] else row['message']
    db.execute('UPDATE native_ha_maintenance SET empty_since=?,state=?,message=? WHERE connection_id=? AND node=?',
               (since, 'ready' if ready else 'evacuating' if status['confirmed'] else row['state'], message, connection_id, node))
    return {**status, 'ready': ready, 'unmanaged': unmanaged(native, residents) if status['confirmed'] else []}


def expose(db, connection_id, node, reason, now):
    """Show a native maintenance problem on a ProxHA maintenance host that no run follows."""
    message = 'Native HA could place guests here because this host is not in native HA maintenance. ' + reason
    db.execute("""UPDATE node_maintenance SET state='blocked',message=?,updated_at=? WHERE connection_id=? AND node=?
        AND enabled=1 AND state!='exiting' AND (state!='blocked' OR message!=?)""", (message, now, connection_id, node, message))


def exclusions(db, connection_id):
    """ProxHA maintenance hosts whose native maintenance no evacuation run follows."""
    running = {r[0] for r in db.execute('SELECT maintenance_node FROM migration_runs WHERE connection_id=? AND finished_at IS NULL AND maintenance_node IS NOT NULL', (connection_id,))}
    hosts = {r[0] for r in db.execute("SELECT node FROM node_maintenance WHERE connection_id=? AND enabled=1 AND state!='exiting'", (connection_id,))}
    return hosts - running


def track(db, connection_id, node, row, status, native, resources, now):
    """Settle and observe native maintenance of a host without an evacuation run."""
    previous = row['state']
    # No foreground call follows this host, so acknowledgement timeouts are decided here.
    row = settle(db, connection_id, node, row, status, now)
    if row['desired'] and not row['owned'] and row['state'] != 'failed' and status['normal'] and status['timestamp'] > row['requested_at']:
        db.execute("UPDATE native_ha_maintenance SET state='failed',message=? WHERE connection_id=? AND node=?", (EXTERNAL_OFF, connection_id, node))
        row = {**row, 'state': 'failed', 'message': EXTERNAL_OFF}
    observe(db, connection_id, node, native, resources, now)
    if row['desired'] and row['state'] == 'failed' and previous != 'failed':
        expose(db, connection_id, node, row['message'], now)


async def preconditions(client, native, now):
    """Reason a background maintenance request must wait, or None."""
    from . import node_maintenance
    if not ha_guests.recent(native.get('ha_manager', {}).get('manager_status', {}).get('timestamp'), now):
        return 'The HA manager status is stale.'
    if any(r.get('armed-state') in ('disarmed', 'disarming') for r in native['ha_status']):
        return 'Native HA is disarmed.'
    quorum = await client.get('cluster/status')
    if not any(r.get('type') == 'cluster' and r.get('quorate') in (1, True) for r in quorum):
        return 'Cluster quorum could not be confirmed.'
    tasks = await client.get('cluster/tasks')
    if any(t.get('type') in ('qmigrate', 'vzmigrate') and not t.get('endtime') and t.get('status') in (None, 'running', 'unknown') for t in tasks):
        return 'Native migrations are active.'
    await node_maintenance.verify_inventory_access(client)
    return None


async def cover(client, db, connection_id, native, resources, now, hosts):
    """Request native maintenance for ProxHA maintenance hosts so the CRM never selects them."""
    eligible = exclusions(db, connection_id)
    keys = {ha_guests.resource(r.get('sid')) for r in native['ha_resources']}
    services = native.get('ha_manager', {}).get('manager_status', {}).get('service_status', {})
    occupied = {r.get('node') for r in resources if r.get('id') in keys} | {s.get('node') for s in services.values()}
    online = {r['node'] for r in resources if r.get('type') == 'node' and r.get('status') == 'online'}
    skipped, requests = {}, []
    for host in sorted(hosts):
        row, status = read(db, connection_id, host), host_state(native, host, now)
        if host not in eligible:
            skipped[host] = ('ProxHA is ending maintenance there or evacuating it.', False)
        elif row:
            track(db, connection_id, host, row, status, native, resources, now)
        elif not native['ha_resources']:
            continue
        elif status['maintenance']:
            # Track maintenance enabled outside ProxHA without taking ownership of it.
            db.execute('INSERT OR IGNORE INTO native_ha_maintenance VALUES(?,?,0,1,?,?,?,NULL)', (connection_id, host, now, 'submitting', EXTERNAL))
            observe(db, connection_id, host, native, resources, now)
        elif host not in online:
            skipped[host] = ('The host is offline.', False)
        elif host in occupied:
            skipped[host] = ('It runs HA resources, so ProxHA does not request native maintenance automatically. '
                             'Resume evacuation or exit maintenance on that host.', True)
        elif not configured():
            skipped[host] = (f'Run ha-manager crm-command node-maintenance enable {host} in Proxmox, or configure native HA SSH.', True)
        else:
            requests.append(host)
    reason = await preconditions(client, native, now) if requests else None
    for host in requests if reason else ():
        skipped[host] = (reason, False)
    for host in () if reason else requests:
        # The row claims the request, so concurrent callers never send it twice.
        if db.execute('INSERT OR IGNORE INTO native_ha_maintenance VALUES(?,?,1,1,?,?,?,NULL)', (connection_id, host, now, 'submitting', COVER)).rowcount != 1:
            continue
        db.commit()
        try:
            await send(db, connection_id, host, client, True)
        except ProxmoxError as exc:
            if not exc.uncertain:
                expose(db, connection_id, host, str(exc), now)
    for host, (reason, operator) in skipped.items():
        if operator:
            expose(db, connection_id, host, reason, now)
    db.commit()
    return skipped


def uncovered(db, connection_id, node, native, now, hosts, skipped):
    """Blocker or wait reason while ProxHA-only maintenance hosts are possible native destinations."""
    details = []
    for host in sorted(hosts):
        row, status = read(db, connection_id, host), host_state(native, host, now)
        reason, operator = skipped.get(host, (None, False))
        if row and row['desired'] and row['state'] == 'failed' and not status['maintenance']:
            reason, operator = row['message'], True
        elif row and row['state'] == 'unknown':
            reason = unresolved(row)
        if operator:
            return ProxmoxError(f'Native HA could select {host}, which ProxHA excludes without native maintenance. '
                                f'Native maintenance on {host} needs attention: {reason} '
                                f'Afterwards resume evacuation of {node}, or constrain the native node-affinity rule.')
        if reason:
            details.append(f'{host}: {reason}')
    return Waiting(f'Waiting for native HA maintenance on {", ".join(sorted(hosts))}, which ProxHA excludes, '
                   f'so native HA cannot place guests from {node} there.' + ''.join(' ' + d for d in details))


async def review(worker, now):
    """Background pass that keeps ProxHA maintenance hosts in native maintenance."""
    with worker.database() as db:
        due = [c for c in {r[0] for r in db.execute('SELECT DISTINCT connection_id FROM node_maintenance WHERE enabled=1')}
               if exclusions(db, c) and (ha_guests.cached(db, c).get('ha_resources') != []
                                         or db.execute('SELECT 1 FROM native_ha_maintenance WHERE connection_id=?', (c,)).fetchone())]
    for connection_id in due:
        if now - worker.native_checks.get(connection_id, 0) < REVIEW_SECONDS:
            continue
        worker.native_checks[connection_id] = now
        try:
            async with worker.lock, worker.client_factory(worker.config_for(connection_id)) as client:
                with worker.database() as db:
                    hosts = exclusions(db, connection_id)
                    entries = await client.get('cluster/ha/resources')
                    if not hosts or entries == [] and not any(read(db, connection_id, h) for h in hosts):
                        continue
                    native = await inspect(client)
                    resources = await client.get('cluster/resources')
                    await cover(client, db, connection_id, native, resources, now, hosts)
        except Exception:
            logging.getLogger(__name__).warning('Native HA maintenance review incomplete for connection %s.', connection_id)


async def ensure_maintenance(client, db, connection_id, node, now, restart_containers):
    entries = await client.get('cluster/ha/resources')
    row = read(db, connection_id, node)
    if entries == [] and not row:
        return True
    native = await inspect(client)
    resources = await client.get('cluster/resources')
    status = host_state(native, node, now)
    previous = row['state'] if row else None
    row = settle(db, connection_id, node, row, status, now)
    if row and row['desired'] and not row['owned'] and row['state'] != 'failed' and status['normal'] and status['timestamp'] > row['requested_at']:
        db.execute('DELETE FROM native_ha_maintenance WHERE connection_id=? AND node=?', (connection_id, node))
        db.commit()
        raise ProxmoxError('Native HA maintenance was disabled outside ProxHA before evacuation finished. '
                           'Resume evacuation to request it from ProxHA, or exit maintenance.')
    if row and row['state'] == 'failed' and row['desired'] and not status['maintenance']:
        if previous not in ('failed', 'unknown'):
            # Report a newly detected failure once; the next resume sends a new request.
            db.commit()
            raise ProxmoxError(row['message'])
        db.execute('DELETE FROM native_ha_maintenance WHERE connection_id=? AND node=?', (connection_id, node))
        row = None
    if row and not row['desired']:
        db.commit()
        raise ProxmoxError('Native HA exit is pending; finish it before starting a new evacuation.')
    if not row:
        if not status['maintenance']:
            pending = await validate_entry(client, db, connection_id, node, native, resources, restart_containers, now)
            if pending:
                # Native HA could choose these hosts; request their native maintenance and recheck later.
                skipped = await cover(client, db, connection_id, native, resources, now, pending)
                raise uncovered(db, connection_id, node, native, now, pending, skipped)
            if time.time() - now > 45:
                raise ProxmoxError('Native HA preflight expired; resume evacuation to recheck current state.')
            await backup_guard.require_clear(client)
            if not configured():
                raise ProxmoxError(f'Native HA is configured. Run ha-manager crm-command node-maintenance enable {node} in Proxmox, then resume; or configure native HA SSH.')
        db.execute('INSERT INTO native_ha_maintenance VALUES(?,?,?,?,?,?,?,NULL)',
                   (connection_id, node, int(not status['maintenance']), 1, now, 'submitting', 'Waiting for native HA maintenance acknowledgement.'))
        db.commit()
        if not status['maintenance']:
            await send(db, connection_id, node, client, True)
    result = observe(db, connection_id, node, native, resources, now)
    if not result.get('ready'):
        row = read(db, connection_id, node)
        if row['state'] in ('unknown', 'failed') and not result['confirmed']:
            db.commit()
            raise ProxmoxError(unresolved(row))
        if result.get('unmanaged'):
            db.commit()
            raise Blocked('Native HA will not evacuate some HA resources. Resolve them in Proxmox, then resume evacuation.', result['unmanaged'])
    return result.get('ready', False)


async def exit_maintenance(client, db, connection_id, node, now):
    entries = await client.get('cluster/ha/resources')
    row = read(db, connection_id, node)
    if entries == [] and not row:
        return True
    native = await inspect(client)
    status = host_state(native, node, now)
    row = settle(db, connection_id, node, row, status, now)
    db.commit()
    if row and row['state'] == 'unknown':
        raise ProxmoxError(unresolved(row))
    # A fresh request may still be applied by the CRM, so it cannot be discarded as off yet.
    requested = bool(row and row['owned'] and row['desired'] and row['state'] == 'submitting')
    if status['normal'] and (not row or row['state'] == 'failed' or not requested and status['timestamp'] > row['requested_at']):
        db.execute('DELETE FROM native_ha_maintenance WHERE connection_id=? AND node=?', (connection_id, node))
        return True
    if requested and not status['maintenance']:
        raise ProxmoxError('Native HA has not recorded the maintenance request yet. Retry shortly; '
                           'ProxHA reports it as not acknowledged after 2 minutes.')
    if not row or not row['owned']:
        raise ProxmoxError('Native HA maintenance was enabled outside ProxHA. Disable it in Proxmox before exiting here.')
    if not row['desired'] and row['state'] != 'failed':
        return False
    if not status['confirmed']:
        raise ProxmoxError('Native HA maintenance has not been confirmed. Resolve its outcome in Proxmox first.')
    tasks = await client.get('cluster/tasks')
    if status['active_services'] or any(t.get('type') in ('qmigrate', 'vzmigrate') and not t.get('endtime') and t.get('status') in (None, 'running', 'unknown') for t in tasks):
        raise ProxmoxError('Wait for native HA evacuation and active migrations to finish before exiting maintenance.')
    await backup_guard.require_clear(client)
    db.execute("UPDATE native_ha_maintenance SET desired=0,requested_at=?,state='exiting',message='Waiting for native HA exit. Native HA may migrate guests back.' WHERE connection_id=? AND node=?", (now, connection_id, node))
    db.commit()
    await send(db, connection_id, node, client, False)
    return False
