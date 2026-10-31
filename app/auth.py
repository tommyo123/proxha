import hashlib
import hmac
import re
import secrets
import threading
from contextlib import contextmanager
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password TEXT NOT NULL, role TEXT NOT NULL, created_at REAL NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, username TEXT, role TEXT,
    method TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log(at);
CREATE TABLE IF NOT EXISTS auth_attempts (
    key TEXT PRIMARY KEY, started_at REAL NOT NULL, attempts INTEGER NOT NULL
);
'''
ROLES = ('viewer', 'operator', 'admin')
SESSION_SECONDS = 12 * 3600
AUDIT_RETENTION = 90 * 86400
COOKIE = 'proxha_session'
HTTP_COOKIE = 'proxha_http_session'
PUBLIC = ('/api/auth/me', '/api/auth/login', '/api/auth/logout')
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)
PASSWORD_SLOTS = threading.BoundedSemaphore(2)
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
# Operators manage guest operations; administrators change configuration.
OPERATE = re.compile(r'^/api/connections/\d+/(operations(/[^/]+/acknowledge)?|sync|placement-rules/(preview|repair)'
                     r'|balancing/(run-now|runs/[^/]+/stop|evaluate|continuous|continuous/(stop|resume))'
                     r'|nodes/[^/]+/(maintenance/(enter|exit)|power|power/[^/]+/acknowledge)|guests/(qemu|lxc)/\d+/start-on-best|capacity-monitor/refresh)$')


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class UserInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(pattern=r'^[A-Za-z0-9._@-]{1,64}$')
    password: str = Field(min_length=1)
    role: Literal['viewer', 'operator', 'admin']


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str | None = Field(default=None, min_length=1)
    role: Literal['viewer', 'operator', 'admin'] | None = None


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


@contextmanager
def password_work():
    if not PASSWORD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, 'Password verification is busy. Try again shortly.', headers={'Retry-After': '2'})
    try:
        yield
    finally:
        PASSWORD_SLOTS.release()


def hash_password(password):
    with password_work():
        return PASSWORD_HASHER.hash(password)


def verify_password(password, stored):
    try:
        with password_work():
            if stored.startswith('scrypt$'):
                _, salt, digest = stored.split('$')
                value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
                return hmac.compare_digest(value.hex(), digest)
            return PASSWORD_HASHER.verify(stored, password)
    except (InvalidHashError, VerificationError, ValueError, TypeError):
        return False


def upgrade(db, now):
    if 'must_change_password' not in {r['name'] for r in db.execute('PRAGMA table_info(users)')}:
        db.execute('ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0')
    if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
        db.execute('INSERT INTO users(username,password,role,created_at,must_change_password) VALUES(?,?,?,?,0)',
                   ('admin', hash_password('password'), 'admin', now))
    db.execute('UPDATE users SET must_change_password=0 WHERE must_change_password!=0')


def throttle(db, username, peer, now):
    db.execute('DELETE FROM auth_attempts WHERE started_at<=?', (now - 60,))
    keys = [(token_key('peer:' + peer), 20), (token_key('user:' + username.casefold()), 10)]
    for key, limit in keys:
        row = db.execute('SELECT attempts,started_at FROM auth_attempts WHERE key=?', (key,)).fetchone()
        if row and row['attempts'] >= limit:
            raise HTTPException(429, 'Too many attempts. Try again in a minute.',
                                headers={'Retry-After': str(max(1, int(row['started_at'] + 60 - now)))})
    for key, _ in keys:
        db.execute('INSERT INTO auth_attempts VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1', (key, now))


def token_key(token):
    # Only a hash of the session token is stored.
    return hashlib.sha256(token.encode()).hexdigest()


def enabled(db):
    return True


def session_user(db, token, now):
    if not token:
        return None
    row = db.execute('''SELECT u.id,u.username,u.role,u.must_change_password FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token=? AND s.expires_at>?''', (token_key(token), now)).fetchone()
    return public_user(row) if row else None


def public_user(row):
    return {'id': row['id'], 'username': row['username'], 'role': row['role'], 'must_change_password': bool(row['must_change_password'])}


def required_role(method, path):
    if path == '/api/auth/password':
        return 'viewer'
    if path.startswith(('/api/users', '/api/audit')):
        return 'admin'
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return 'viewer'
    return 'operator' if OPERATE.match(path) else 'admin'


def allowed(role, required):
    return ROLES.index(role) >= ROLES.index(required)


def login(db, credentials, now):
    row = db.execute('SELECT * FROM users WHERE username=?', (credentials.username,)).fetchone()
    # Hash even for unknown users so response time does not reveal valid names.
    valid = verify_password(credentials.password, row['password'] if row else DUMMY_HASH)
    if not row or not valid:
        return None, None
    upgraded = hash_password(credentials.password) if row['password'].startswith('scrypt$') or PASSWORD_HASHER.check_needs_rehash(row['password']) else None
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
    if not current or current['password'] != row['password']:
        return None, None
    if upgraded:
        db.execute('UPDATE users SET password=? WHERE id=?', (upgraded, row['id']))
    return new_session(db, current, now)


def new_session(db, user, now):
    token = secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
    db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_key(token), user['id'], now, now + SESSION_SECONDS))
    return token, public_user(user)


def change_password(db, user_id, value, now):
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user or not verify_password(value.current_password, user['password']):
        raise HTTPException(400, 'The current password is incorrect.')
    changed = db.execute('UPDATE users SET password=?,must_change_password=0 WHERE id=? AND password=?',
                         (hash_password(value.new_password), user_id, user['password'])).rowcount
    if changed != 1:
        raise HTTPException(409, 'Your password changed in another session. Sign in again.')
    db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    return new_session(db, {**dict(user), 'must_change_password': 0}, now)


def logout(db, token):
    if token:
        db.execute('DELETE FROM sessions WHERE token=?', (token_key(token),))


def list_users(db):
    return [dict(r) for r in db.execute('SELECT id,username,role,created_at,must_change_password FROM users ORDER BY username')]


def admins(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


def create_user(db, data, now):
    try:
        user_id = db.execute('INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,?)',
                             (data.username, hash_password(data.password), data.role, now)).lastrowid
    except Exception as exc:
        if 'UNIQUE' in str(exc):
            raise HTTPException(409, 'A user with this name already exists.') from None
        raise
    return next(u for u in list_users(db) if u['id'] == user_id)


def update_user(db, user_id, data):
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        raise HTTPException(404, 'User not found.')
    if data.role and data.role != 'admin' and user['role'] == 'admin' and admins(db) == 1:
        raise HTTPException(409, 'Keep at least one administrator.')
    if data.role:
        db.execute('UPDATE users SET role=? WHERE id=?', (data.role, user_id))
    if data.password:
        db.execute('UPDATE users SET password=?,must_change_password=0 WHERE id=?', (hash_password(data.password), user_id))
    if data.password or data.role and data.role != user['role']:
        db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    return next(u for u in list_users(db) if u['id'] == user_id)


def delete_user(db, user_id):
    user = db.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        raise HTTPException(404, 'User not found.')
    if user['role'] == 'admin' and admins(db) == 1:
        raise HTTPException(409, 'Keep at least one administrator.')
    db.execute('DELETE FROM users WHERE id=?', (user_id,))


def audit(db, now, user, method, path, status):
    db.execute('INSERT INTO audit_log(at,username,role,method,path,status) VALUES(?,?,?,?,?,?)',
               (now, user['username'] if user else None, user['role'] if user else None, method, path, status))
    db.execute('DELETE FROM audit_log WHERE at<?', (now - AUDIT_RETENTION,))


def audit_log(db, limit):
    return [dict(r) for r in db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,))]
