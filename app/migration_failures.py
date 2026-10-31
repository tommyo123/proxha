import math

from . import telemetry

POINTS = 200
SCHEMA = '''
CREATE TABLE IF NOT EXISTS migration_failures (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
    occurred_at REAL NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS migration_failures_age ON migration_failures(connection_id,occurred_at);
'''


def record(db, step, message, now):
    db.execute('''INSERT OR IGNORE INTO migration_failures
        SELECT ?,connection_id,?,?,?,?,? FROM migration_runs WHERE id=?''',
        (step['operation_id'], step['resource'], step['source'], step['target'], now, message, step['run_id']))


def costs(db, connection_id, now):
    result = {}
    for row in db.execute('''SELECT * FROM migration_failures
            WHERE connection_id=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at''',
            (connection_id, now - telemetry.RETENTION, now)):
        detail = result.setdefault(row['resource'], {'penalty': 0, 'failures_7d': 0, 'last_failed_at': None,
                                                     'penalty_per_minute': 1, 'decay_minutes': POINTS})
        detail['penalty'] += max(0, POINTS - math.floor((now - row['occurred_at']) / 60))
        detail.update(failures_7d=detail['failures_7d'] + 1, last_failed_at=row['occurred_at'], message=row['message'])
    return result


def prune(db, now):
    db.execute('DELETE FROM migration_failures WHERE occurred_at<?', (now - telemetry.RETENTION,))
