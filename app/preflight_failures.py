import math

from . import telemetry

SCHEMA = '''
CREATE TABLE IF NOT EXISTS preflight_failures (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
    attempts INTEGER NOT NULL, failed_at REAL NOT NULL, retry_at REAL NOT NULL, message TEXT NOT NULL,
    PRIMARY KEY(connection_id,resource,source,target)
);
'''


def record(db, connection_id, move, message, now):
    key = (connection_id, move['resource'], move['source'], move['target'])
    old = db.execute('SELECT attempts,failed_at FROM preflight_failures WHERE connection_id=? AND resource=? AND source=? AND target=?', key).fetchone()
    attempts = old['attempts'] + 1 if old and 0 <= now - old['failed_at'] < 3600 else 1
    delay = min(900, 120 * 2 ** min(3, attempts - 1))
    db.execute('INSERT OR REPLACE INTO preflight_failures VALUES(?,?,?,?,?,?,?,?)', (*key, attempts, now, now + delay, message))


def pending(db, connection_id, now):
    return {(r['resource'], r['source'], r['target']): dict(r) for r in db.execute(
        'SELECT * FROM preflight_failures WHERE connection_id=? AND failed_at<=? AND retry_at>?', (connection_id, now, now))}


def reason(failures, resource, source, target, now):
    failure = failures.get((resource, source, target))
    if failure:
        return f'Preflight retry in {math.ceil(failure["retry_at"] - now)} seconds: {failure["message"]}'
    return None


def prune(db, now):
    db.execute('DELETE FROM preflight_failures WHERE failed_at<?', (now - telemetry.RETENTION,))
