import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .proxmox import ConnectionInput, ProxmoxClient, ProxmoxError, fetch_cluster, fetch_cluster_ca, segment
from .operations import OperationInput, preflight
from . import cluster_ca
from . import endpoints
from . import native_ha
from . import capacity
from . import capacity_monitor
from . import infrastructure
from . import runtime
from . import __version__
from uuid import UUID, uuid5
from . import activity, adaptive, auth, background, backup_guard, balancing, continuous, free_balloon, guest_policy, migration_groups, migration_history, migration_outcomes, migration_poll, migration_queue, node_maintenance, notifications, placement_rules, planning_schedule, pressure_policy, replay, resource_groups, scheduled, start_placement, storage, telemetry

ROOT = Path(__file__).resolve().parent.parent
DATA = runtime.config_dir()


@contextmanager
def database():
    db = sqlite3.connect(DATA / 'proxha.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    runtime.check_legacy_data(DATA)
    with endpoints.LOCK:
        endpoints.POOLS.clear()
    DATA.mkdir(parents=True, exist_ok=True)
    key_path = DATA / 'secret.key'
    if not key_path.exists():
        with key_path.open('xb') as f:
            f.write(Fernet.generate_key())
        key_path.chmod(0o600)
    app.state.cipher = Fernet(key_path.read_bytes())
    with database() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                token_id TEXT NOT NULL, secret BLOB NOT NULL, verify_tls INTEGER NOT NULL,
                snapshot TEXT, synced_at TEXT, last_error TEXT, trusted_ca TEXT);
            CREATE TABLE IF NOT EXISTS machines (
                connection_id INTEGER REFERENCES connections(id) ON DELETE CASCADE,
                resource_id TEXT, kind TEXT, node TEXT, data TEXT,
                PRIMARY KEY(connection_id, resource_id));
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY, connection_id INTEGER REFERENCES connections(id) ON DELETE CASCADE,
                name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL, enabled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, connection_id INTEGER REFERENCES connections(id) ON DELETE CASCADE,
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_tasks (
                id INTEGER PRIMARY KEY, operation_id TEXT REFERENCES operations(id) ON DELETE CASCADE,
                vmid INTEGER NOT NULL, name TEXT, node TEXT, kind TEXT, upid TEXT,
                status TEXT NOT NULL, message TEXT);
            PRAGMA user_version=2;
        ''')
        db.execute("UPDATE operation_tasks SET status='unknown', message='Submission interrupted. Check Proxmox tasks before retrying.' WHERE status='submitting'")
        if 'trusted_ca' not in {r['name'] for r in db.execute('PRAGMA table_info(connections)')}:
            db.execute('ALTER TABLE connections ADD COLUMN trusted_ca TEXT')
        db.executescript(telemetry.SCHEMA)
        telemetry.upgrade(db)
        db.executescript(migration_groups.SCHEMA)
        db.executescript(resource_groups.SCHEMA)
        db.executescript(guest_policy.SCHEMA)
        db.executescript(balancing.SCHEMA)
        placement_rules.upgrade(db)
        db.executescript(activity.SCHEMA)
        db.executescript(migration_queue.SCHEMA)
        db.executescript(continuous.SCHEMA)
        db.executescript(scheduled.SCHEMA)
        db.executescript(notifications.SCHEMA)
        db.executescript(auth.SCHEMA)
        auth.upgrade(db, telemetry.time.time())
        db.executescript(endpoints.SCHEMA)
        db.executescript(native_ha.SCHEMA)
        db.executescript(capacity_monitor.SCHEMA)
        migration_queue.upgrade(db)
        migration_queue.recover(db, telemetry.time.time())
        db.execute("UPDATE native_ha_maintenance SET state='unknown',message='Native HA submission was interrupted. Check native HA state; no command will be replayed.' WHERE state IN ('submitting','exiting')")
        continuous.recover(db, telemetry.time.time())
        scheduled.recover(db, telemetry.time.time())
        migration_queue.prune(db, telemetry.time.time())
        db.execute('PRAGMA user_version=28')
        telemetry.prune(db, telemetry.time.time())
        balancing.prune(db, telemetry.time.time())
        activity.prune(db, telemetry.time.time())


@asynccontextmanager
async def lifespan(app):
    initialize()
    app.state.operation_lock = asyncio.Lock()
    app.state.activity_lock = asyncio.Lock()
    app.state.replay_lock = asyncio.Lock()
    app.state.collector = telemetry.Collector(database, config_for, after_tick=lambda now: (endpoints.persist(database), balancing.tick(database, now)))
    app.state.activity = activity.Worker(database, config_for, lock=app.state.activity_lock)
    app.state.storage = storage.Worker(database, config_for)
    app.state.migration_queue = migration_queue.Worker(database, config_for, app.state.operation_lock, client_factory=lambda config: ProxmoxClient(config))
    app.state.continuous = continuous.Worker(database, app.state.operation_lock)
    app.state.notifications = notifications.Worker(database, lambda: app.state.cipher)
    app.state.infrastructure = infrastructure.Worker(database)
    app.state.capacity_monitor = capacity_monitor.Worker(database, config_for, client_factory=lambda config: ProxmoxClient(config))
    if os.environ.get('PROXHA_COLLECTOR_ENABLED', '1') != '0':
        app.state.collector.start()
        app.state.activity.start()
        app.state.storage.start()
        app.state.migration_queue.start()
        app.state.continuous.start()
        app.state.notifications.start()
        app.state.infrastructure.start()
        app.state.capacity_monitor.start()
    try:
        yield
    finally:
        await app.state.capacity_monitor.stop()
        await app.state.notifications.stop()
        await app.state.infrastructure.stop()
        await app.state.continuous.stop()
        await app.state.migration_queue.stop()
        await app.state.collector.stop()
        await app.state.activity.stop()
        await app.state.storage.stop()
        await background.run(endpoints.persist, database)


app = FastAPI(title='ProxHA', version=__version__, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get('PROXHA_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1],testserver').split(','))


@app.middleware('http')
async def local_security(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        try:
            foreign = origin and (urlsplit(origin).netloc != request.headers.get('host') or urlsplit(origin).scheme != request.url.scheme)
        except ValueError:
            foreign = True
        if request.headers.get('x-proxha-request') != '1' or foreign:
            return JSONResponse({'detail': 'Request must originate from ProxHA.'}, status_code=403)
    path, user = request.url.path, None
    guarded = path.startswith('/api/') and path not in auth.PUBLIC
    changing = request.method not in ('GET', 'HEAD', 'OPTIONS')
    response = None
    if guarded:
        with database() as db:
            user = auth.session_user(db, request.cookies.get(session_cookie(request)), telemetry.time.time())
        required = auth.required_role(request.method, path)
        if not user:
            response = JSONResponse({'detail': 'Sign in to ProxHA.'}, status_code=401)
        elif not auth.allowed(user['role'], required):
            response = JSONResponse({'detail': f'This action requires the {required} role.'}, status_code=403)
    request.state.user = user
    if response is None:
        response = await call_next(request)
    if changing and (guarded or path in ('/api/auth/login', '/api/auth/logout')):
        with database() as db:
            auth.audit(db, telemetry.time.time(), request.state.user, request.method, path, response.status_code)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    elif request.url.path == '/' or request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache'
    return response


@app.get('/api/auth/me')
def auth_me(request: Request):
    with database() as db:
        user = auth.session_user(db, request.cookies.get(session_cookie(request)), telemetry.time.time())
    return {'auth_enabled': True, 'user': user}


@app.post('/api/auth/login')
def auth_login(credentials: auth.Credentials, request: Request):
    now = telemetry.time.time()
    request.state.user = {'username': credentials.username, 'role': None}
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        auth.throttle(db, credentials.username, request.client.host if request.client else 'unknown', now)
    with database() as db:
        token, user = auth.login(db, credentials, now)
    if user:
        request.state.user = user
    if not user:
        raise HTTPException(401, 'Unknown user name or wrong password.')
    return session_response(request, token, user)


def session_cookie(request):
    return auth.COOKIE if request.url.scheme == 'https' else auth.HTTP_COOKIE


def session_response(request, token, user):
    response = JSONResponse({'user': user})
    response.set_cookie(session_cookie(request), token, max_age=auth.SESSION_SECONDS, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https', path='/')
    return response


@app.post('/api/auth/password')
def change_password(value: auth.PasswordChange, request: Request):
    now, user = telemetry.time.time(), request.state.user
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        auth.throttle(db, user['username'], request.client.host if request.client else 'unknown', now)
    with database() as db:
        token, updated = auth.change_password(db, user['id'], value, now)
    return session_response(request, token, updated)


@app.post('/api/auth/logout')
def auth_logout(request: Request):
    with database() as db:
        request.state.user = auth.session_user(db, request.cookies.get(session_cookie(request)), telemetry.time.time())
        auth.logout(db, request.cookies.get(session_cookie(request)))
    response = JSONResponse({'signed_out': True})
    response.delete_cookie(session_cookie(request), path='/', secure=request.url.scheme == 'https', httponly=True, samesite='strict')
    return response


@app.get('/api/users')
def users():
    with database() as db:
        return {'users': auth.list_users(db)}


@app.post('/api/users', status_code=201)
def create_user(value: auth.UserInput):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        return auth.create_user(db, value, telemetry.time.time())


@app.put('/api/users/{user_id}')
def update_user(user_id: int, value: auth.UserUpdate):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        return auth.update_user(db, user_id, value)


@app.delete('/api/users/{user_id}', status_code=204)
def delete_user(user_id: int):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        auth.delete_user(db, user_id)


@app.get('/api/audit')
def audit_log(limit: int = Query(200, ge=1, le=1000)):
    with database() as db:
        return {'entries': auth.audit_log(db, limit)}


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Never echo submitted credentials from Pydantic's input field.
    errors = [f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({'detail': '; '.join(errors)}, status_code=422)


def row_for(connection_id):
    with database() as db:
        row = db.execute('SELECT * FROM connections WHERE id=?', (connection_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Connection not found.')
    return row


def public(row):
    return {**{k: row[k] for k in ('id', 'name', 'url', 'token_id', 'verify_tls', 'synced_at', 'last_error')},
            'trusted_ca': cluster_ca.summary(row['trusted_ca'])}


def stored_config(row):
    return ConnectionInput(**{k: row[k] for k in ('name', 'url', 'token_id', 'verify_tls', 'trusted_ca')},
                           token_secret=app.state.cipher.decrypt(row['secret']).decode())


def resolve_ca(config, saved=None, confirm=True):
    """None keeps the saved CA and an empty value removes it; a new CA needs its confirmed fingerprint."""
    if config.trusted_ca is None:
        config.trusted_ca = saved
    elif not config.trusted_ca:
        config.trusted_ca = None
    elif confirm and config.trusted_ca != saved and not cluster_ca.confirmed(config.trusted_ca, config.trusted_ca_fingerprint):
        raise HTTPException(422, 'Compare the cluster CA fingerprint on a cluster host and confirm it before saving.')
    config.trusted_ca_fingerprint = None
    if config.trusted_ca:
        config.verify_tls = True
    return config


def store_snapshot(db, connection_id, snapshot):
    if 'api_routing' in snapshot:
        endpoints.remember(db, connection_id, snapshot.pop('api_routing'))
    db.execute('UPDATE connections SET snapshot=?,synced_at=?,last_error=NULL WHERE id=?',
               (json.dumps(snapshot), datetime.now(timezone.utc).isoformat(), connection_id))
    db.execute('DELETE FROM machines WHERE connection_id=?', (connection_id,))
    db.executemany('INSERT INTO machines VALUES (?,?,?,?,?)',
                   [(connection_id, r['id'], r['type'], r.get('node'), json.dumps(r))
                    for r in snapshot['nodes'] + snapshot['guests']])


@app.get('/api/connections')
def connections():
    with database() as db:
        return [{**public(r), 'api_routing': endpoints.summary(db, r['id'], r['url'])}
                for r in db.execute('SELECT * FROM connections ORDER BY id')]


async def check(config):
    try:
        return await fetch_cluster(config)
    except ProxmoxError as exc:
        raise HTTPException(502, str(exc)) from None


async def read_cluster_ca(config):
    try:
        return {**await fetch_cluster_ca(config), 'url': config.url}
    except ProxmoxError as exc:
        message = str(exc)
        if getattr(exc, 'certificate_rejected', False) and not config.trusted_ca:
            message += (' A self-signed cluster cannot be verified before its CA is trusted: clear Verify TLS certificate'
                        ' for this first read, or paste the CA certificate.')
        raise HTTPException(502, message) from None


@app.post('/api/connections/cluster-ca/describe')
def describe_cluster_ca(value: cluster_ca.PemInput):
    return cluster_ca.describe(value.pem)


@app.post('/api/connections/cluster-ca/fetch')
async def fetch_new_cluster_ca(config: ConnectionInput):
    """Shows the CA of a cluster that is not saved yet; nothing is stored."""
    if not config.token_secret:
        raise HTTPException(422, 'API token secret is required.')
    return await read_cluster_ca(resolve_ca(config, confirm=False))


@app.post('/api/connections/{connection_id}/cluster-ca/fetch')
async def fetch_saved_cluster_ca(connection_id: int, config: ConnectionInput):
    """Shows the CA through the dialog's settings; a blank secret uses the saved token. Nothing is stored."""
    row = row_for(connection_id)
    if not config.token_secret:
        config.token_secret = app.state.cipher.decrypt(row['secret']).decode()
    return await read_cluster_ca(resolve_ca(config, row['trusted_ca'], confirm=False))


@app.post('/api/connections', status_code=201)
async def create_connection(config: ConnectionInput):
    if not config.token_secret:
        raise HTTPException(422, 'API token secret is required.')
    resolve_ca(config)
    snapshot = await check(config)
    with database() as db:
        connection_id = db.execute('INSERT INTO connections(name,url,token_id,secret,verify_tls,trusted_ca) VALUES (?,?,?,?,?,?)',
                                  (config.name, config.url, config.token_id, app.state.cipher.encrypt(config.token_secret.encode()), config.verify_tls, config.trusted_ca)).lastrowid
        endpoints.remember(db, connection_id, None)
        store_snapshot(db, connection_id, snapshot)
    return public(row_for(connection_id))


@app.put('/api/connections/{connection_id}')
async def update_connection(connection_id: int, config: ConnectionInput):
    async with app.state.activity_lock, app.state.operation_lock:
        row = row_for(connection_id)
        with database() as db:
            if migration_queue.active(db, connection_id):
                raise HTTPException(409, 'Stop the migration queue before editing this connection.')
            if db.execute("SELECT 1 FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status IN ('submitting','running','unknown')", (connection_id,)).fetchone():
                raise HTTPException(409, 'Resolve active or unknown tasks before editing this connection.')
            if row['url'] != config.url and node_maintenance.excluded(db, connection_id):
                raise HTTPException(409, 'Exit host maintenance before changing the cluster URL; maintenance state belongs to this connection.')
        saved_secret = app.state.cipher.decrypt(row['secret']).decode()
        new_token = bool(config.token_secret)
        if not config.token_secret:
            config.token_secret = saved_secret
        resolve_ca(config, row['trusted_ca'])
        credentials_changed = (row['token_id'] != config.token_id or bool(row['verify_tls']) != config.verify_tls or saved_secret != config.token_secret
                               or row['trusted_ca'] != config.trusted_ca)
        if config.url == row['url'] and not new_token and not credentials_changed:
            # Saving keeps discovered peers and checks endpoints rejected for a cluster mismatch again.
            # A new URL, token, TLS setting or trusted CA starts a fresh identity check from the configured URL.
            with database() as db:
                endpoints.configure(db, connection_id, config, revalidate=True)
        snapshot = await check(config)
        with database() as db:
            db.execute('UPDATE connections SET name=?,url=?,token_id=?,secret=?,verify_tls=?,trusted_ca=? WHERE id=?',
                       (config.name, config.url, config.token_id, app.state.cipher.encrypt(config.token_secret.encode()), config.verify_tls, config.trusted_ca, connection_id))
            endpoints.remember(db, connection_id, None)
            store_snapshot(db, connection_id, snapshot)
            if credentials_changed:
                db.execute('DELETE FROM guest_storage WHERE connection_id=?', (connection_id,))
            if row['url'] != config.url:
                for table in ('scheduled_executions', 'native_tasks', 'activity_status', 'backup_status', 'task_scans', 'guest_locations', 'placement_events',
                              'metric_samples', 'metric_observations', 'collector_status', 'balancing_evaluations', 'balancing_schedule_state', 'guest_storage', 'migration_runs', 'node_maintenance', 'native_ha_maintenance', 'node_power_actions', 'continuous_state', 'preflight_failures',
                              'infrastructure_sources', 'infrastructure_samples', 'infrastructure_minutes',
                              'capacity_settings', 'capacity_state', 'capacity_events'):
                    db.execute(f'DELETE FROM {table} WHERE connection_id=?', (connection_id,))
                continuous.change(db, connection_id, telemetry.time.time(), enabled=False)
                config = balancing.settings(db, connection_id)
                revision = config.pop('revision')
                config['distribution_enabled'] = False
                config['container_schedule'] = {**config.get('container_schedule', planning_schedule.DEFAULTS['container_schedule']), 'enabled': False}
                config['placement_repair_enabled'] = False
                db.execute('DELETE FROM placement_intents WHERE connection_id=?', (connection_id,))
                db.execute('DELETE FROM placement_failures WHERE connection_id=?', (connection_id,))
                db.execute('UPDATE placement_rules SET enabled=0,revision=revision+1 WHERE connection_id=?', (connection_id,))
                db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)', (connection_id, json.dumps(config), revision + 1))
                db.execute('UPDATE migration_groups SET enabled=0,revision=revision+1,updated_at=? WHERE connection_id=?',
                           (datetime.now(timezone.utc).isoformat(), connection_id))
    return public(row_for(connection_id))


@app.get('/api/connections/{connection_id}/cluster')
def cluster(connection_id: int):
    row = row_for(connection_id)
    snapshot = json.loads(row['snapshot']) if row['snapshot'] else None
    if snapshot:
        with database() as db:
            for node in snapshot.get('nodes', []):
                state = node_maintenance.read(db, connection_id, node['node'])
                if state['enabled']:
                    node['maintenance'] = state['state']
    return {'connection': public(row), 'cluster': snapshot}


@app.post('/api/connections/{connection_id}/sync')
async def sync(connection_id: int):
    row = row_for(connection_id)
    config = stored_config(row)
    with database() as db:
        endpoints.configure(db, connection_id, config)
    try:
        snapshot = await fetch_cluster(config)
    except ProxmoxError as exc:
        with database() as db:
            db.execute('UPDATE connections SET last_error=? WHERE id=?', (str(exc), connection_id))
        raise HTTPException(502, str(exc)) from None
    with database() as db:
        current = db.execute('SELECT * FROM connections WHERE id=?', (connection_id,)).fetchone()
        if not current:
            raise HTTPException(404, 'Connection was removed.')
        if all(current[key] == row[key] for key in ('url', 'token_id', 'secret', 'verify_tls', 'trusted_ca')):
            store_snapshot(db, connection_id, snapshot)
    return cluster(connection_id)


@app.delete('/api/connections/{connection_id}', status_code=204)
async def remove_connection(connection_id: int):
    async with app.state.activity_lock, app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            if migration_queue.active(db, connection_id):
                raise HTTPException(409, 'Stop the migration queue before removing this connection.')
            if db.execute("SELECT 1 FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status IN ('submitting','running','unknown')", (connection_id,)).fetchone():
                raise HTTPException(409, 'Resolve active or unknown tasks before removing this connection.')
            if node_maintenance.excluded(db, connection_id):
                raise HTTPException(409, 'Exit host maintenance before removing this connection.')
            endpoints.remember(db, connection_id, None)
            db.execute('DELETE FROM connections WHERE id=?', (connection_id,))


def config_for(connection_id):
    row = row_for(connection_id)
    config = stored_config(row)
    with database() as db:
        endpoints.configure(db, connection_id, config)
    return config


def operation_result(operation_id):
    with database() as db:
        row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Operation not found.')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['tasks'] = [dict(t) for t in db.execute('SELECT * FROM operation_tasks WHERE operation_id=? ORDER BY id', (operation_id,))]
    return result


@app.post('/api/connections/{connection_id}/operations')
async def submit_operation(connection_id: int, operation: OperationInput, request: Request):
    return await execute_operation(connection_id, operation, request)


async def execute_operation(connection_id, operation, request, start_plan=None):
    user = request.state.user
    # Free balloon runs a QEMU monitor command through a token with Sys.Modify on /.
    if operation.action == 'free-balloon' and user and user['role'] != 'admin':
        raise HTTPException(403, 'Free balloon requires the admin role.')
    operation_id = str(operation.request_id)
    payload = operation.model_dump_json()
    if start_plan is not None:
        payload = json.dumps({**json.loads(payload), 'start_placement': start_plan})
    async with app.state.operation_lock:
        with database() as db:
            existing = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if existing:
                if existing['connection_id'] != connection_id or existing['payload'] != payload:
                    raise HTTPException(409, 'Request ID is already used by another operation.')
                return operation_result(operation_id)
            if operation.action == 'migrate' and migration_queue.active(db, connection_id):
                raise HTTPException(409, 'Stop the migration queue before starting another migration.')
            if operation.action == 'migrate' and operation.target in node_maintenance.excluded(db, connection_id):
                raise HTTPException(409, 'Migration target is in ProxHA maintenance mode.')
            active = db.execute("SELECT t.vmid FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status IN ('submitting','running','unknown')", (connection_id,)).fetchall()
            if set(operation.vmids).intersection(t['vmid'] for t in active):
                raise HTTPException(409, 'A selected guest has an active or unresolved task. Check task history first.')
            if operation.action == 'free-balloon':
                if migration_queue.active(db, connection_id):
                    raise HTTPException(409, 'Wait for the migration queue to finish or stop it before using Free balloon.')
                try:
                    migration_queue.ensure_idle(db, connection_id)
                except ProxmoxError:
                    raise HTTPException(409, 'Wait for active operations to finish and resolve unknown outcomes before using Free balloon.') from None
        async with ProxmoxClient(config_for(connection_id)) as client:
            try:
                prepared = (await start_placement.prepare(client, database, connection_id, operation, start_plan)
                            if start_plan is not None else await preflight(client, operation))
                if operation.action == 'migrate':
                    with database() as db:
                        bandwidth = migration_queue.bwlimit(balancing.settings(db, connection_id))
                        for _, _, params in prepared:
                            params.update(bandwidth)
                        await placement_rules.live_check(client, db, connection_id,
                            {guest['id']: operation.target for guest, _, _ in prepared},
                            maintenance=all(guest['node'] in node_maintenance.excluded(db, connection_id) for guest, _, _ in prepared))
                if operation.action == 'start':
                    with database() as db:
                        excluded = node_maintenance.excluded(db, connection_id)
                        await placement_rules.live_check(client, db, connection_id,
                            {guest['id']: guest['node'] for guest, _, _ in prepared}, starting=True)
                    if any(guest['node'] in excluded for guest, _, _ in prepared):
                        raise ProxmoxError('Guests cannot be started on a host in ProxHA maintenance mode.')
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None
            with database() as db:
                db.execute('INSERT INTO operations VALUES (?,?,?,?)', (operation_id, connection_id, payload, datetime.now(timezone.utc).isoformat()))
                task_ids = [db.execute('INSERT INTO operation_tasks(operation_id,vmid,name,node,kind,status) VALUES (?,?,?,?,?,?)',
                                      (operation_id, g['vmid'], g.get('name'), g['node'], g['type'], 'submitting')).lastrowid for g, _, _ in prepared]
                if operation.action == 'free-balloon':
                    for task_id, (_, _, params) in zip(task_ids, prepared):
                        db.execute('INSERT INTO balloon_requests VALUES(?,?,?)',
                                   (task_id, int(params['command'].split()[1]) * free_balloon.MIB, telemetry.time.time()))
            halted = False
            for task_id, (guest, path, params) in zip(task_ids, prepared):
                upid = None
                if halted:
                    status, message = 'skipped', 'Not submitted because a previous submission failed.'
                else:
                    try:
                        if operation.action == 'migrate':
                            await backup_guard.require_clear(client)
                        response = await client.post(path, params)
                        if operation.action == 'free-balloon':
                            free_balloon.accepted(response, client.redact)
                            status, message = 'running', 'Free balloon requested; waiting for full RAM allocation to be confirmed.'
                        else:
                            upid = response
                            if not isinstance(upid, str) or not upid.startswith('UPID:'):
                                raise ProxmoxError('No task ID returned. Check Proxmox before retrying.', uncertain=True)
                            status, message = 'running', 'Accepted by Proxmox; waiting for task completion.'
                    except ProxmoxError as exc:
                        status, message = ('unknown' if exc.uncertain else 'failed'), str(exc)
                        if operation.action == 'free-balloon' and exc.status_code == 403:
                            message += ' Free balloon requires Sys.Modify on / for both the user and API token.'
                        upid, halted = None, True
                with database() as db:
                    db.execute('UPDATE operation_tasks SET upid=?,status=?,message=? WHERE id=?', (upid, status, message, task_id))
            if operation.action == 'free-balloon':
                await free_balloon.reconcile(database, connection_id, client)
    return operation_result(operation_id)


@app.get('/api/connections/{connection_id}/guests/{kind}/{vmid}/start-placement')
def start_recommendation(connection_id: int, kind: Literal['qemu', 'lxc'], vmid: int):
    connection = row_for(connection_id)
    with database() as db:
        return start_placement.recommend(db, connection_id, json.loads(connection['snapshot'] or '{}'), f'{kind}/{vmid}', telemetry.time.time())


@app.post('/api/connections/{connection_id}/guests/{kind}/{vmid}/start-on-best')
async def start_on_best_host(connection_id: int, kind: Literal['qemu', 'lxc'], vmid: int, value: migration_queue.RunInput, request: Request):
    row_for(connection_id)
    resource = f'{kind}/{vmid}'
    migration_id, start_id = (str(uuid5(value.request_id, action)) for action in ('migrate', 'start'))
    plan, migration = None, None
    with database() as db:
        for operation_id in (migration_id, start_id):
            saved = db.execute('SELECT connection_id,payload FROM operations WHERE id=?', (operation_id,)).fetchone()
            if saved:
                plan = json.loads(saved['payload']).get('start_placement')
                if saved['connection_id'] != connection_id or not plan or plan['resource'] != resource:
                    raise HTTPException(409, 'Request ID is already used by another operation.')
                if operation_id == migration_id:
                    migration = operation_result(operation_id)
                else:
                    return {'placement': plan, 'migration': migration, 'start': operation_result(operation_id)}
    try:
        async with ProxmoxClient(config_for(connection_id)) as client:
            if plan is None:
                plan = await start_placement.live_recommend(client, database, connection_id, resource, telemetry.time.time())
            if not plan['target']:
                raise ProxmoxError('No host can start this guest: ' + '; '.join(f'{o["node"]}: {o["reason"]}' for o in plan['options']))
            if plan['migrate']:
                migration = await execute_operation(connection_id, OperationInput(request_id=migration_id, action='migrate', vmids=[vmid], target=plan['target']), request, plan)
                task = migration['tasks'][0]
                if task['status'] not in ('running', 'completed') or not task['upid']:
                    raise ProxmoxError(f'Migration to {plan["target"]} was not accepted: {task["message"]}')
                # A retry after a verified migration goes straight to the start step.
                if task['status'] == 'running':
                    failures = 0
                    for _ in range(150):
                        try:
                            status, message = await migration_poll.read(client, {**task, 'resource': resource, 'target': plan['target']}, 'stopped')
                            failures = 0
                            if status != 'running':
                                break
                        except ProxmoxError as exc:
                            failures += 1
                            if not isinstance(exc, migration_poll.PlacementPending) and not exc.retryable or failures >= migration_poll.MAX_FAILURES:
                                raise
                        await asyncio.sleep(2)
                    else:
                        raise ProxmoxError('Offline migration is still running; retry this request when it finishes.')
                    with database() as db:
                        db.execute("UPDATE operation_tasks SET status=?,message=? WHERE id=? AND status='running'", (status, message, task['id']))
                    if status != 'completed':
                        raise ProxmoxError(f'Offline migration failed: {message}. The guest was not started.')
                with database() as db:
                    start_placement.settle_migration(db, connection_id, task, resource, plan, telemetry.time.time())
        try:
            start = await execute_operation(connection_id, OperationInput(request_id=start_id, action='start', vmids=[vmid]), request, plan)
        except HTTPException as exc:
            if not plan['migrate'] or exc.status_code != 409:
                raise
            raise HTTPException(409, f'{resource} was moved to {plan["target"]} and is stopped there; the start was not submitted: {exc.detail} '
                                     'Retry the same request to start it without another migration.') from None
    except ProxmoxError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'placement': plan, 'migration': migration, 'start': start}


@app.get('/api/connections/{connection_id}/operations')
async def list_operations(connection_id: int):
    config = config_for(connection_id)
    with database() as db:
        ids = [r['id'] for r in db.execute('SELECT id FROM operations WHERE connection_id=? ORDER BY created_at DESC LIMIT 50', (connection_id,))]
        tasks = [dict(t) for t in db.execute("SELECT t.* FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status='running'", (connection_id,))]
    if tasks:
        async with ProxmoxClient(config) as client:
            await activity.reconcile(database, connection_id, client)
    return [operation_result(i) for i in ids]


@app.post('/api/connections/{connection_id}/operations/{operation_id}/acknowledge')
async def acknowledge_unknown(connection_id: int, operation_id: str):
    async with app.state.operation_lock:
        result = operation_result(operation_id)
        if result['connection_id'] != connection_id:
            raise HTTPException(404, 'Operation not found.')
        with database() as db:
            db.execute("UPDATE operation_tasks SET status='acknowledged', message='Unknown outcome acknowledged by user after checking Proxmox.' WHERE operation_id=? AND status='unknown'", (operation_id,))
    return operation_result(operation_id)


@app.get('/')
def index():
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    for name in ('style.css', 'console.css', 'selection.js', 'management.js', 'insights.js', 'migration-outcomes.js', 'groups.js', 'resource-groups.js', 'placement-rules.js', 'drs.js', 'workspace.js', 'capacity.js', 'app.js'):
        version = hashlib.sha256((ROOT / 'static' / name).read_bytes()).hexdigest()[:16]
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={version}"')
    return HTMLResponse(html)


@app.get('/api/connections/{connection_id}/telemetry')
def telemetry_latest(connection_id: int):
    row_for(connection_id)
    with database() as db:
        cutoff = int(telemetry.time.time()) - telemetry.RETENTION
        samples = telemetry.latest(db, connection_id, telemetry.time.time())
        status = db.execute('SELECT * FROM collector_status WHERE connection_id=?', (connection_id,)).fetchone()
        count = db.execute('SELECT COUNT(*),MIN(sampled_at),MAX(sampled_at) FROM metric_samples WHERE connection_id=? AND sampled_at>=?', (connection_id, cutoff)).fetchone()
    return {'samples': samples, 'collector': dict(status) if status else None,
            'stored_samples': count[0], 'oldest_sample': count[1], 'newest_sample': max((s['sampled_at'] for s in samples), default=None),
            'interval_seconds': telemetry.INTERVAL, 'summary_interval_seconds': telemetry.SUMMARY_INTERVAL,
            'raw_retention_hours': 24, 'retention_days': 7}


@app.get('/api/inventory')
def inventory_search(q: str = Query(min_length=1, max_length=100)):
    query = q.casefold().strip()
    with database() as db:
        rows = db.execute('SELECT m.*,c.name AS connection_name FROM machines m JOIN connections c ON c.id=m.connection_id ORDER BY c.name,m.resource_id LIMIT 10000').fetchall()
    matches = []
    for row in rows:
        data = json.loads(row['data'])
        name = data.get('name') or data.get('node') or row['resource_id']
        if query in f'{name} {row["resource_id"]} {row["node"]} {row["connection_name"]}'.casefold():
            matches.append({'resource': row['resource_id'], 'name': name, 'node': row['node'],
                            'connection_id': row['connection_id'], 'connection_name': row['connection_name']})
        if len(matches) == 30:
            break
    return {'items': matches}


@app.get('/api/connections/{connection_id}/balancing')
def balancing_status(connection_id: int):
    row_for(connection_id)
    with database() as db:
        result = balancing.latest(db, connection_id)
        configuration = balancing.settings(db, connection_id)
        schedule = planning_schedule.describe(db, connection_id, configuration, telemetry.time.time())
        schedule['execution'] = scheduled.latest(db, connection_id)
        container_schedule = planning_schedule.describe(db, connection_id, configuration, telemetry.time.time(), guest_kind='lxc')
        container_schedule['execution'] = scheduled.latest(db, connection_id, guest_kind='lxc')
        run = migration_queue.document(db, connection_id)
        automatic = continuous.document(db, connection_id)
    return {'latest': result, 'interval_seconds': configuration['continuous_interval_seconds'] if configuration['continuous_enabled'] else balancing.INTERVAL, 'retention_days': 7,
            'automatic_execution_enabled': (configuration['continuous_enabled'] or configuration['distribution_enabled'] or configuration['container_schedule']['enabled']) and not configuration['paused'], 'manual_execution_enabled': True, 'run': run, 'settings': configuration, 'schedule': schedule, 'container_schedule': container_schedule, 'continuous': automatic}


@app.put('/api/connections/{connection_id}/balancing/continuous')
async def set_continuous_enabled(connection_id: int, value: continuous.EnabledInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            if balancing.settings(db, connection_id)['revision'] != value.revision:
                raise HTTPException(409, 'Settings changed in another session. Refresh before changing continuous balancing.')
            continuous.change(db, connection_id, telemetry.time.time(), enabled=value.enabled)
            result = {'settings': balancing.settings(db, connection_id), 'continuous': continuous.document(db, connection_id)}
    app.state.continuous.wakeup.set()
    return result


@app.post('/api/connections/{connection_id}/balancing/continuous/{action}')
async def continuous_action(connection_id: int, action: Literal['stop', 'resume']):
    row_for(connection_id)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        continuous.change(db, connection_id, telemetry.time.time(), enabled=action == 'resume', resume=action == 'resume')
        result = continuous.document(db, connection_id)
    app.state.continuous.wakeup.set()
    return result


@app.post('/api/connections/{connection_id}/balancing/evaluate')
async def evaluate_balancing(connection_id: int, guest_kind: Literal['qemu', 'lxc'] = 'qemu'):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            result = balancing.save_evaluation(db, connection_id, telemetry.time.time(), guest_kind=guest_kind)
    return result


@app.put('/api/connections/{connection_id}/balancing/continuous/aggressiveness')
async def save_continuous_aggressiveness(connection_id: int, value: adaptive.AggressivenessInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            config = balancing.settings(db, connection_id)
            if config.pop('revision') != value.revision:
                raise HTTPException(409, 'Settings changed in another session. Refresh before saving.')
            config['continuous_aggressiveness'] = value.aggressiveness
            db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)', (connection_id, json.dumps(config), value.revision + 1))
            continuous.change(db, connection_id, telemetry.time.time())
            result = {'settings': balancing.settings(db, connection_id), 'continuous': continuous.document(db, connection_id)}
    app.state.continuous.wakeup.set()
    return result


@app.post('/api/connections/{connection_id}/balancing/run-now')
async def run_perfect_balancing(connection_id: int, value: migration_queue.RunInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            try:
                return {'run': migration_queue.start(db, connection_id, str(value.request_id), telemetry.time.time(),
                               guest_kind=value.guest_kind,
                               trigger='balloon_recovery' if value.goal == 'balloon_recovery' else 'aggressive_manual' if value.aggressive else 'manual')}
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None


@app.get('/api/connections/{connection_id}/capacity')
async def capacity_report(connection_id: int, memory_mode: Literal['configured', 'observed'] = 'configured'):
    row_for(connection_id)
    async with ProxmoxClient(config_for(connection_id)) as client:
        try:
            await node_maintenance.verify_inventory_access(client)
            resources, native = await asyncio.gather(client.get('cluster/resources'), native_ha.inspect(client))
        except ProxmoxError as exc:
            raise HTTPException(409, str(exc)) from None
    def calculate():
        with database() as db:
            return capacity.evaluate(db, connection_id, resources, native, telemetry.time.time(), memory_mode)
    return await asyncio.to_thread(calculate)


@app.get('/api/connections/{connection_id}/capacity-monitor')
def capacity_monitor_status(connection_id: int):
    row_for(connection_id)
    with database() as db:
        return capacity_monitor.read(db, connection_id, telemetry.time.time())


@app.put('/api/connections/{connection_id}/capacity-monitor')
async def capacity_monitor_settings(connection_id: int, value: capacity_monitor.SettingsInput):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            capacity_monitor.save(db, connection_id, value)
    app.state.capacity_monitor.wakeup.set()
    return capacity_monitor_status(connection_id)


@app.post('/api/connections/{connection_id}/capacity-monitor/refresh')
async def refresh_capacity_monitor(connection_id: int):
    row_for(connection_id)
    with database() as db:
        if not capacity_monitor.settings(db, connection_id)['enabled']:
            raise HTTPException(409, 'Enable automatic capacity checks before requesting a refresh.')
        db.execute('DELETE FROM capacity_state WHERE connection_id=?', (connection_id,))
    app.state.capacity_monitor.wakeup.set()
    return capacity_monitor_status(connection_id)


@app.get('/api/connections/{connection_id}/infrastructure')
def infrastructure_status(connection_id: int):
    connection = row_for(connection_id)
    with database() as db:
        sources = infrastructure.sources(db, connection_id)
        nodes = sorted({n['node'] for n in json.loads(connection['snapshot'] or '{}').get('nodes', [])} | sources.keys())
        return {'nodes': nodes, 'sources': sources,
                'latest': infrastructure.latest(db, connection_id, telemetry.time.time())}


@app.put('/api/connections/{connection_id}/infrastructure/{node}')
async def infrastructure_source(connection_id: int, value: infrastructure.SourceInput, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    async with app.state.operation_lock:
        connection = row_for(connection_id)
        with database() as db:
            previous = infrastructure.sources(db, connection_id).get(node, {})
            if node not in {n['node'] for n in json.loads(connection['snapshot'] or '{}').get('nodes', [])} and (value.enabled or not previous):
                raise HTTPException(422, 'Choose a host from the connected cluster. Retired sources can only be disabled.')
            if previous.get('revision', 0) != value.revision:
                raise HTTPException(409, 'Exporter configuration changed; reload before saving.')
            db.execute('INSERT OR REPLACE INTO infrastructure_sources VALUES(?,?,?,?)',
                       (connection_id, node, value.model_dump_json(exclude={'revision'}), value.revision + 1))
            db.execute('DELETE FROM infrastructure_samples WHERE connection_id=? AND node=?', (connection_id, node))
        return infrastructure_status(connection_id)


@app.get('/api/connections/{connection_id}/infrastructure/{node}/history')
def infrastructure_history(connection_id: int, node: str, hours: int = Query(default=24, ge=1, le=168)):
    row_for(connection_id)
    with database() as db:
        rows = [{'sampled_at': r['sampled_at'], **json.loads(r['data'])} for r in db.execute(
            'SELECT sampled_at,data FROM infrastructure_minutes WHERE connection_id=? AND node=? AND sampled_at>=? ORDER BY sampled_at',
            (connection_id, node, telemetry.time.time() - hours * 3600))]
    return {'node': node, 'hours': hours, 'retention_days': 7, 'samples': rows}


@app.get('/api/connections/{connection_id}/balancing/run')
def migration_run_status(connection_id: int):
    row_for(connection_id)
    with database() as db:
        return {'run': migration_queue.document(db, connection_id), 'continuous': continuous.document(db, connection_id)}


@app.post('/api/connections/{connection_id}/balancing/runs/{run_id}/stop')
def stop_migration_run(connection_id: int, run_id: str):
    row_for(connection_id)
    with database() as db:
        result = migration_queue.stop(db, connection_id, run_id, telemetry.time.time())
        if result is None:
            raise HTTPException(404, 'Migration queue not found.')
        return {'run': result}


@app.get('/api/connections/{connection_id}/notifications')
def notification_settings(connection_id: int):
    row_for(connection_id)
    with database() as db:
        return notifications.read(db, connection_id, app.state.cipher)


@app.put('/api/connections/{connection_id}/notifications')
def save_notification_settings(connection_id: int, value: notifications.SettingsInput):
    row_for(connection_id)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        return notifications.save(db, connection_id, value, app.state.cipher)


@app.post('/api/connections/{connection_id}/notifications/test')
async def send_test_notification(connection_id: int):
    row_for(connection_id)
    now = telemetry.time.time()
    with database() as db:
        if not notifications.webhook(notifications.row(db, connection_id), app.state.cipher):
            raise HTTPException(422, 'Save a webhook URL before sending a test.')
        notifications.queue(db, connection_id, 'test', 'Notifications from ProxHA reach this webhook.', now)
        item = db.execute('SELECT MAX(id) FROM notifications WHERE connection_id=?', (connection_id,)).fetchone()[0]
    async with app.state.notifications.client_factory() as client:
        results = await notifications.deliver(database, connection_id, app.state.cipher, client, now, only=item)
    if results:
        return results[0]
    # Nothing was sent because the settings changed after the test was queued.
    error = 'Notification settings changed before the test was sent. Send the test again.'
    with database() as db:
        db.execute('UPDATE notifications SET error=? WHERE id=? AND delivered_at IS NULL', (error, item))
    return {'id': item, 'delivered': False, 'error': error}


@app.post('/api/connections/{connection_id}/balancing/replay')
async def replay_balancing(connection_id: int, value: replay.ReplayInput):
    return await run_replay_job(connection_id, value, replay.run)


@app.post('/api/connections/{connection_id}/balancing/replay/compare')
async def compare_balancing(connection_id: int, value: replay.ComparisonInput):
    return await run_replay_job(connection_id, value, replay.compare)


async def run_replay_job(connection_id, value, function):
    row_for(connection_id)
    if app.state.replay_lock.locked():
        raise HTTPException(429, 'Another replay is running. Wait for it to finish before starting another.', headers={'Retry-After': '30'})

    def work():
        with database() as db:
            return function(db, connection_id, value, telemetry.time.time())
    try:
        async with app.state.replay_lock:
            return await background.run(work)
    except ValidationError as exc:
        raise HTTPException(422, '; '.join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from None


@app.get('/api/connections/{connection_id}/balancing/cpu-performance')
def cpu_performance(connection_id: int):
    row_for(connection_id)
    with database() as db:
        return {'factors': balancing.performance(db, connection_id)}


@app.put('/api/connections/{connection_id}/balancing/cpu-performance')
async def save_cpu_performance(connection_id: int, value: balancing.PerformanceInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            return {'factors': balancing.save_performance(db, connection_id, value.factors)}


@app.put('/api/connections/{connection_id}/balancing/settings')
async def save_balancing_settings(connection_id: int, value: balancing.SettingsInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            if balancing.settings(db, connection_id)['revision'] != value.revision:
                raise HTTPException(409, 'Settings changed in another session. Refresh before saving.')
            if 'container_schedule' not in value.model_fields_set:
                value.container_schedule = balancing.ContainerSchedule.model_validate(balancing.settings(db, connection_id)['container_schedule'])
            for key in ('maintenance_concurrency', 'maintenance_memory_mode', 'distribution_effort', 'scheduled_minimum_score', 'container_balancing_enabled', *adaptive.DEFAULTS, *pressure_policy.DEFAULTS):
                if key not in value.model_fields_set:
                    setattr(value, key, balancing.settings(db, connection_id)[key])
            db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)',
                       (connection_id, value.model_dump_json(exclude={'revision'}), value.revision + 1))
            continuous.change(db, connection_id, telemetry.time.time())
            app.state.continuous.wakeup.set()
            balancing.save_evaluation(db, connection_id, telemetry.time.time())
            return balancing.settings(db, connection_id)


@app.get('/api/connections/{connection_id}/balancing/history')
def balancing_history(connection_id: int):
    row_for(connection_id)
    with database() as db:
        rows = db.execute('SELECT result FROM balancing_evaluations WHERE connection_id=? AND evaluated_at>=? ORDER BY evaluated_at DESC LIMIT 200',
                          (connection_id, telemetry.time.time() - telemetry.RETENTION)).fetchall()
    return {'evaluations': [{'evaluated_at': d['evaluated_at'], 'strategy': d.get('strategy', 'gradual'), 'status': d['status'],
                             'candidates': len(d.get('candidates', [])), 'planned_moves': len(d.get('plan', [])),
                             'evaluation_kind': d.get('evaluation_kind', 'legacy'), 'diagnostic_moves': len(d.get('diagnostic_plan', [])),
                             'top_score': max((c.get('score') or 0 for c in d.get('candidates', [])), default=None),
                             'automatic_execution_enabled': d.get('automatic_execution_enabled', False)} for d in (json.loads(row['result']) for row in rows)], 'retention_days': 7}


@app.get('/api/connections/{connection_id}/nodes/{node}/evacuation-preview')
def evacuation_preview(connection_id: int, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    connection = row_for(connection_id)
    if not any(n['node'] == node for n in json.loads(connection['snapshot'] or '{}').get('nodes', [])):
        raise HTTPException(404, 'Host not found.')
    with database() as db:
        return balancing.evaluate(db, connection_id, telemetry.time.time(), maintenance_source=node)


@app.get('/api/connections/{connection_id}/host-status')
async def host_status(connection_id: int):
    async with ProxmoxClient(config_for(connection_id)) as client:
        try:
            resources = await client.get('cluster/resources')
        except ProxmoxError as exc:
            raise HTTPException(502, str(exc)) from None
    nodes = []
    with database() as db:
        for host in resources:
            if host.get('type') != 'node':
                continue
            maintenance = node_maintenance.read(db, connection_id, host['node'])
            guests = node_maintenance.residents(resources, host['node'])
            nodes.append({'node': host['node'], 'status': host.get('status', 'unknown'),
                          'maintenance': maintenance['state'] if maintenance['enabled'] else None,
                          'maintenance_idle': maintenance['enabled'] and all(g.get('status') == 'stopped' for g in guests)})
    return {'nodes': nodes}


@app.get('/api/connections/{connection_id}/nodes/{node}/maintenance')
async def node_maintenance_status(connection_id: int, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    row_for(connection_id)
    async with ProxmoxClient(config_for(connection_id)) as client:
        try:
            with database() as db:
                result = await node_maintenance.refresh(client, db, connection_id, node, telemetry.time.time())
                result['run'] = migration_queue.document(db, connection_id, result['run_id']) if result['run_id'] else None
                current = migration_queue.active(db, connection_id)
                result['queue_active'] = bool(current)
                result['concurrency'] = balancing.settings(db, connection_id)['maintenance_concurrency']
                result['memory_mode'] = balancing.settings(db, connection_id)['maintenance_memory_mode']
                return result
        except ProxmoxError as exc:
            raise HTTPException(409, str(exc)) from None


@app.post('/api/connections/{connection_id}/nodes/{node}/maintenance/enter')
async def enter_node_maintenance(connection_id: int, value: node_maintenance.EnterInput, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    row_for(connection_id)
    async with app.state.operation_lock:
        async with ProxmoxClient(config_for(connection_id)) as client:
            try:
                await node_maintenance.verify_inventory_access(client)
                resources = await client.get('cluster/resources')
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None
        if not any(r.get('type') == 'node' and r.get('node') == node and r.get('status') == 'online' for r in resources):
            raise HTTPException(409, 'Host must be online and visible before entering maintenance.')
        with database() as db:
            power = node_maintenance.read(db, connection_id, node)['power_action']
            if power and power['status'] in ('submitting', 'accepted', 'unknown'):
                raise HTTPException(409, 'Resolve the pending host power action before evacuation.')
            try:
                run = migration_queue.start(db, connection_id, str(value.request_id), telemetry.time.time(), node, value.restart_containers)
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None
            if run['active']:
                db.execute('''INSERT INTO node_maintenance(connection_id,node,enabled,state,message,updated_at,run_id) VALUES(?,?,1,'evacuating',?,?,?)
                    ON CONFLICT(connection_id,node) DO UPDATE SET enabled=1,state='evacuating',message=excluded.message,updated_at=excluded.updated_at,run_id=excluded.run_id,blockers='[]' ''',
                    (connection_id, node, 'Host excluded from placement. Evacuating with the configured parallel migration limit.', telemetry.time.time(), run['id']))
            return {'maintenance': node_maintenance.read(db, connection_id, node), 'run': run}


@app.post('/api/connections/{connection_id}/nodes/{node}/maintenance/exit')
async def exit_node_maintenance(connection_id: int, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            current = migration_queue.active(db, connection_id)
            if current and current['maintenance_node'] == node:
                raise HTTPException(409, 'Stop the evacuation queue and wait for its current migration before exiting maintenance.')
            if db.execute("""SELECT 1 FROM migration_run_steps s JOIN migration_runs r ON r.id=s.run_id
                JOIN operation_tasks t ON t.operation_id=s.operation_id WHERE r.connection_id=? AND r.maintenance_node=?
                AND t.status IN ('submitting','running','unknown')""", (connection_id, node)).fetchone():
                raise HTTPException(409, 'Resolve outstanding evacuation tasks before exiting maintenance.')
            power = node_maintenance.read(db, connection_id, node)['power_action']
            if power and power['status'] in ('submitting', 'accepted', 'unknown'):
                raise HTTPException(409, 'Wait for the host power action to complete or resolve its unknown outcome first.')
        async with ProxmoxClient(config_for(connection_id)) as client:
            try:
                resources = await client.get('cluster/resources')
                with database() as db:
                    if not any(r.get('type') == 'node' and r.get('node') == node and r.get('status') == 'online' for r in resources):
                        raise ProxmoxError('Host must be online before exiting maintenance.')
                    if not await native_ha.exit_maintenance(client, db, connection_id, node, telemetry.time.time()):
                        node_maintenance.set_state(db, connection_id, node, 'exiting', 'Waiting for native HA exit. Native HA may migrate evacuated guests back.', telemetry.time.time())
                        return node_maintenance.read(db, connection_id, node)
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None
        if not any(r.get('type') == 'node' and r.get('node') == node and r.get('status') == 'online' for r in resources):
            raise HTTPException(409, 'Host must be online before exiting maintenance.')
        with database() as db:
            db.execute("UPDATE node_maintenance SET enabled=0,state='normal',message='Maintenance ended. Host is eligible for future balancing.',updated_at=?,blockers='[]' WHERE connection_id=? AND node=?", (telemetry.time.time(), connection_id, node))
            return node_maintenance.read(db, connection_id, node)


@app.post('/api/connections/{connection_id}/nodes/{node}/power')
async def node_power(connection_id: int, value: node_maintenance.PowerInput, node: str = PathParam(pattern=r'^[A-Za-z0-9][A-Za-z0-9.-]*$')):
    row_for(connection_id)
    if value.confirm_node != node:
        raise HTTPException(422, 'Type the exact host name to confirm its power action.')
    identifier = str(value.request_id)
    async with app.state.operation_lock:
        with database() as db:
            previous = db.execute('SELECT * FROM node_power_actions WHERE id=?', (identifier,)).fetchone()
            if previous:
                if (previous['connection_id'], previous['node'], previous['command']) != (connection_id, node, value.command):
                    raise HTTPException(409, 'Request ID belongs to another host power action.')
                return dict(previous)
            if db.execute("SELECT 1 FROM node_power_actions WHERE connection_id=? AND status IN ('submitting','accepted','unknown')", (connection_id,)).fetchone():
                raise HTTPException(409, 'A host power action is active or unresolved. Check its status before submitting another.')
        async with ProxmoxClient(config_for(connection_id)) as client:
            try:
                with database() as db:
                    uptime = await node_maintenance.power_preflight(client, db, connection_id, node)
            except ProxmoxError as exc:
                raise HTTPException(409, str(exc)) from None
            now = telemetry.time.time()
            with database() as db:
                db.execute('INSERT INTO node_power_actions VALUES(?,?,?,?,?,?,?,?,?)', (identifier, connection_id, node, value.command, 'submitting', 'Submitting host power action.', now, now, uptime))
            try:
                response = await client.post(f'nodes/{segment(node)}/status', {'command': value.command})
                if response is not None:
                    raise ProxmoxError('Unexpected power response. Verify the host before retrying.', uncertain=True)
                status, message = 'accepted', f'{value.command.title()} accepted by Proxmox. Waiting to verify host state; maintenance remains enabled.'
            except ProxmoxError as exc:
                status, message = ('unknown' if exc.uncertain else 'failed'), str(exc)
            with database() as db:
                db.execute('UPDATE node_power_actions SET status=?,message=?,updated_at=? WHERE id=?', (status, message, telemetry.time.time(), identifier))
                return dict(db.execute('SELECT * FROM node_power_actions WHERE id=?', (identifier,)).fetchone())


@app.post('/api/connections/{connection_id}/nodes/{node}/power/{action_id}/acknowledge')
async def acknowledge_node_power(connection_id: int, node: str, action_id: str):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            row = db.execute('SELECT * FROM node_power_actions WHERE id=? AND connection_id=? AND node=?', (action_id, connection_id, node)).fetchone()
            if not row:
                raise HTTPException(404, 'Host power action not found.')
            if row['status'] != 'unknown':
                raise HTTPException(409, 'Only an unknown outcome can be acknowledged.')
            db.execute("UPDATE node_power_actions SET status='acknowledged',message='User verified host state and acknowledged the unknown outcome.',updated_at=? WHERE id=?", (telemetry.time.time(), action_id))
            return {'acknowledged': True}


@app.get('/api/connections/{connection_id}/activity')
def activity_list(connection_id: int, resource: str | None = Query(default=None, pattern=r'^(node/[A-Za-z0-9.-]+|(?:qemu|lxc)/[0-9]+)$')):
    row_for(connection_id)
    with database() as db:
        return activity.document(db, connection_id, resource)


@app.get('/api/connections/{connection_id}/activity/log')
async def activity_log(connection_id: int, upid: str = Query(min_length=5, max_length=512)):
    row_for(connection_id)
    with database() as db:
        task = db.execute('SELECT node FROM native_tasks WHERE connection_id=? AND upid=?', (connection_id, upid)).fetchone()
    if not task:
        raise HTTPException(404, 'Task not found in this connection.')
    async with ProxmoxClient(config_for(connection_id)) as client:
        try:
            lines = await client.get(f'nodes/{segment(task["node"])}/tasks/{segment(upid)}/log?limit=200')
            return {'lines': [client.redact(str(line.get('t', ''))) for line in lines]}
        except ProxmoxError as exc:
            raise HTTPException(502, str(exc)) from None


@app.get('/api/connections/{connection_id}/history')
def telemetry_history(connection_id: int,
                      resource: str = Query(default='datacenter', pattern=r'^(datacenter|node/[A-Za-z0-9.-]+|(?:qemu|lxc)/[0-9]+)$'),
                      hours: int = Query(default=24, ge=1, le=168)):
    row_for(connection_id)
    with database() as db:
        return telemetry.history(db, connection_id, resource, hours)


@app.get('/api/connections/{connection_id}/resources/{kind}/{identifier}')
async def detail_view(connection_id: int, kind: Literal['node', 'qemu', 'lxc'],
                      identifier: str = PathParam(pattern=r'^[A-Za-z0-9.-]+$')):
    try:
        async with ProxmoxClient(config_for(connection_id)) as client:
            async with asyncio.timeout(30):
                result = await telemetry.resource_detail(client, kind, identifier)
                if kind != 'node':
                    with database() as db:
                        result['automatic_migrations'] = migration_history.automatic(db, connection_id, f'{kind}/{identifier}', telemetry.time.time())
                else:
                    with database() as db:
                        result['extra']['infrastructure'] = infrastructure.latest(db, connection_id, telemetry.time.time()).get(identifier)
                return result
    except ProxmoxError as exc:
        raise HTTPException(502, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, 'Resource details timed out.') from None


@app.get('/api/connections/{connection_id}/resource-groups')
def list_resource_groups(connection_id: int):
    connection = row_for(connection_id)
    with database() as db:
        return resource_groups.document(db, connection)


@app.post('/api/connections/{connection_id}/resource-groups', status_code=201)
async def create_resource_group(connection_id: int, group: resource_groups.GroupInput):
    async with app.state.operation_lock:
        connection = row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            return resource_groups.save(db, connection, group)


@app.put('/api/connections/{connection_id}/resource-groups/{group_id}')
async def update_resource_group(connection_id: int, group_id: int, group: resource_groups.GroupUpdate):
    async with app.state.operation_lock:
        connection = row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            return resource_groups.save(db, connection, group, group_id)


@app.delete('/api/connections/{connection_id}/resource-groups/{group_id}', status_code=204)
async def delete_resource_group(connection_id: int, group_id: int, revision: int = Query(ge=1)):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            resource_groups.delete(db, connection_id, group_id, revision)


def rule_statuses(db, connection_id, snapshot):
    policy = placement_rules.Policy(db, connection_id, snapshot.get('nodes', []) + snapshot.get('guests', []))
    native = db.execute('SELECT native_policy FROM activity_status WHERE connection_id=?', (connection_id,)).fetchone()
    conflicts = placement_rules.ha_conflicts(policy.rules + policy.preferences + policy.reported, json.loads(native['native_policy'] or '{}') if native else {})
    statuses = policy.statuses()
    for status in statuses:
        rule = next(r for r in policy.rules + policy.preferences + policy.reported if r['id'] == status['id'])
        pinned = [r for r in rule['guests'] if r in policy.guests and guest_policy.read(db, connection_id, r)['stickiness'] == 10]
        if pinned and status['enforcement'] != 'report':
            action = 'repair' if status['enforcement'] == 'must' else 'balancing'
            status['warnings'].append(f'Stickiness 10 prevents automatic {action} of {", ".join(pinned[:5])}{" and others" if len(pinned) > 5 else ""}.')
        status['warnings'] += conflicts.get(status['id'], [])
    return statuses


@app.get('/api/connections/{connection_id}/placement-rules')
def list_placement_rules(connection_id: int):
    connection = row_for(connection_id)
    snapshot = json.loads(connection['snapshot'] or '{}')
    with database() as db:
        return {'rules': placement_rules.list_rules(db, connection_id), 'statuses': rule_statuses(db, connection_id, snapshot),
                'groups': resource_groups.list_groups(db, connection_id), 'synced_at': connection['synced_at'],
                'repair_enabled': balancing.settings(db, connection_id)['placement_repair_enabled'],
                'parallel_limit': balancing.settings(db, connection_id)['placement_concurrency']}


def repair_plan(db, connection_id, now):
    # Repair is evaluated as if automatic repair were enabled; callers roll back or keep the change.
    config = balancing.settings(db, connection_id)
    if not config['placement_repair_enabled']:
        revision = config.pop('revision')
        db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)', (connection_id, json.dumps({**config, 'placement_repair_enabled': True}), revision + 1))
    return placement_rules.repair_summary(balancing.evaluate(db, connection_id, now, run_now=True, rule_only=True))


@app.post('/api/connections/{connection_id}/placement-rules/preview')
async def preview_placement_rule(connection_id: int, rule: placement_rules.PreviewInput):
    connection = row_for(connection_id)
    snapshot = json.loads(connection['snapshot'] or '{}')
    now = telemetry.time.time()
    async with app.state.operation_lock:
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                repair_enabled = balancing.settings(db, connection_id)['placement_repair_enabled']
                before, _, _ = repair_plan(db, connection_id, now)
                saved = placement_rules.save(db, connection_id, rule, rule.rule_id)
                status = next((s for s in rule_statuses(db, connection_id, snapshot) if s['id'] == saved['id']), None)
                moves, blocked, message = repair_plan(db, connection_id, now)
            finally:
                db.rollback()
    known = {(tuple(m['members']), m['target']) for m in before}
    for move in moves:
        move['new'] = (tuple(move['members']), move['target']) not in known
    return {'rule': saved, 'status': status, 'moves': moves, 'blocked': blocked, 'message': message, 'repair_enabled': repair_enabled}


@app.post('/api/connections/{connection_id}/placement-rules/repair')
async def repair_placement_rules(connection_id: int, value: migration_queue.RunInput):
    row_for(connection_id)
    async with app.state.operation_lock:
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            now = telemetry.time.time()
            existing = db.execute('SELECT 1 FROM migration_runs WHERE id=?', (str(value.request_id),)).fetchone()
            moves, blocked, message = ([], [], '') if existing else repair_plan(db, connection_id, now)
            if not existing and not moves:
                db.rollback()
                return {'run': None, 'moves': [], 'blocked': blocked, 'message': message}
            try:
                run = migration_queue.start(db, connection_id, str(value.request_id), now, trigger='placement_rules',
                                            restart_containers=balancing.settings(db, connection_id)['container_balancing_enabled'])
            except ProxmoxError as exc:
                db.rollback()
                raise HTTPException(409, str(exc)) from None
            return {'run': run, 'moves': moves, 'blocked': blocked, 'message': message}


@app.post('/api/connections/{connection_id}/placement-rules', status_code=201)
async def create_placement_rule(connection_id: int, rule: placement_rules.RuleInput):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            return placement_rules.save(db, connection_id, rule)


@app.put('/api/connections/{connection_id}/placement-rules/{rule_id}')
async def update_placement_rule(connection_id: int, rule_id: int, rule: placement_rules.RuleUpdate):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            return placement_rules.save(db, connection_id, rule, rule_id)


@app.delete('/api/connections/{connection_id}/placement-rules/{rule_id}', status_code=204)
async def delete_placement_rule(connection_id: int, rule_id: int, revision: int = Query(ge=1)):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            placement_rules.delete(db, connection_id, rule_id, revision)


@app.get('/api/connections/{connection_id}/migration-groups')
def live_migration_groups(connection_id: int):
    connection = row_for(connection_id)
    with database() as db:
        return migration_groups.document(db, connection)


@app.get('/api/connections/{connection_id}/guests/{kind}/{vmid}/automatic-migrations')
def automatic_migration_history(connection_id: int, kind: Literal['qemu', 'lxc'], vmid: int = PathParam(ge=100, le=999999999)):
    row_for(connection_id)
    with database() as db:
        return migration_history.automatic(db, connection_id, f'{kind}/{vmid}', telemetry.time.time())


@app.get('/api/connections/{connection_id}/guests/{kind}/{vmid}/automatic-migrations/{operation_id}/outcome')
def automatic_migration_outcome(connection_id: int, kind: Literal['qemu', 'lxc'], operation_id: UUID,
                               vmid: int = PathParam(ge=100, le=999999999)):
    row_for(connection_id)
    with database() as db:
        result = migration_outcomes.report(db, connection_id, f'{kind}/{vmid}', str(operation_id), telemetry.time.time())
    if result is None:
        raise HTTPException(404, 'Verified automatic migration not found within the seven-day history.')
    return result


@app.get('/api/connections/{connection_id}/guests/{kind}/{vmid}/policy')
def get_guest_policy(connection_id: int, kind: Literal['qemu', 'lxc'], vmid: int = PathParam(ge=100, le=999999999)):
    resource = guest_policy.resource_for(row_for(connection_id), kind, vmid)
    with database() as db:
        return guest_policy.read(db, connection_id, resource)


@app.put('/api/connections/{connection_id}/guests/{kind}/{vmid}/policy')
async def update_guest_policy(connection_id: int, kind: Literal['qemu', 'lxc'], policy: guest_policy.PolicyInput,
                              vmid: int = PathParam(ge=100, le=999999999)):
    async with app.state.operation_lock:
        resource = guest_policy.resource_for(row_for(connection_id), kind, vmid)
        with database() as db:
            return guest_policy.save(db, connection_id, resource, policy)


@app.post('/api/connections/{connection_id}/migration-groups', status_code=201)
async def create_migration_group(connection_id: int, group: migration_groups.GroupInput):
    async with app.state.operation_lock:
        connection = row_for(connection_id)
        with database() as db:
            return migration_groups.save_group(db, connection, group)


@app.put('/api/connections/{connection_id}/migration-groups/{group_id}')
async def update_migration_group(connection_id: int, group_id: int, group: migration_groups.GroupUpdate):
    async with app.state.operation_lock:
        connection = row_for(connection_id)
        with database() as db:
            return migration_groups.save_group(db, connection, group, group_id)


@app.delete('/api/connections/{connection_id}/migration-groups/{group_id}', status_code=204)
async def delete_migration_group(connection_id: int, group_id: int, revision: int = Query(ge=1)):
    async with app.state.operation_lock:
        row_for(connection_id)
        with database() as db:
            group = migration_groups.group_for(db, connection_id, group_id)
            if group['revision'] != revision:
                raise HTTPException(409, 'This group changed in another session. Refresh before removing it.')
            db.execute('DELETE FROM migration_groups WHERE id=?', (group_id,))


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
