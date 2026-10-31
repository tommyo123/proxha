from uuid import NAMESPACE_URL, uuid5

from . import backup_guard, balancing, migration_queue, planning_schedule, telemetry
from .proxmox import ProxmoxError

SCHEMA = '''CREATE TABLE IF NOT EXISTS scheduled_executions (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    schedule_key TEXT NOT NULL, slot REAL NOT NULL, created_at REAL NOT NULL,
    status TEXT NOT NULL, message TEXT NOT NULL, run_id TEXT, finished_at REAL,
    PRIMARY KEY(connection_id, schedule_key, slot)
);'''


def latest(db, connection_id, guest_kind='qemu'):
    match = 'LIKE' if guest_kind == 'lxc' else 'NOT LIKE'
    row = db.execute(f"SELECT * FROM scheduled_executions WHERE connection_id=? AND schedule_key {match} 'lxc/%' ORDER BY created_at DESC, slot DESC LIMIT 1",
                     (connection_id,)).fetchone()
    return dict(row) if row else None


def due(db, connection_id, config, now, guest_kind='qemu'):
    schedule = planning_schedule.describe(db, connection_id, config, now, guest_kind=guest_kind)
    if not schedule['enabled'] or config['paused'] or schedule['slot'] is None or not schedule['slot'] <= now < schedule['window_end']:
        return None
    seen = db.execute('SELECT 1 FROM scheduled_executions WHERE connection_id=? AND schedule_key=? AND slot=?',
                      (connection_id, schedule['schedule_key'], schedule['slot'])).fetchone()
    return None if seen else schedule


def reserved(db, connection_id, config, now):
    return bool(db.execute("SELECT 1 FROM scheduled_executions WHERE connection_id=? AND finished_at IS NULL",
                           (connection_id,)).fetchone() or any(due(db, connection_id, config, now, kind) for kind in ('qemu', 'lxc')))


def reset_continuous(db, connection_id, now, holding):
    from . import continuous
    state = continuous.read(db, connection_id)
    state.update(observation=None, candidate=None, ready_at=None, next_check_at=now,
                 history_reset_at=now, schedule_hold=holding, suspended=False, failure=None, error_count=0, retry_at=None)
    if holding:
        state.update(status='scheduled_paused', message='Scheduled balancing has priority. Waiting for its queue to finish.')
    else:
        state.update(next_move_at=max(state.get('next_move_at') or 0, now + 60),
                     status='settling', message='Scheduled balancing finished. Waiting for fresh measurements and a new assessment.')
    if not balancing.settings(db, connection_id)['continuous_enabled']:
        state.update(status='disabled', message='Continuous balancing is disabled.')
    continuous.save(db, connection_id, state, now)


def finish(db, row, status, message, now):
    db.execute('UPDATE scheduled_executions SET status=?,message=?,finished_at=? WHERE connection_id=? AND schedule_key=? AND slot=?',
               (status, message, now, row['connection_id'], row['schedule_key'], row['slot']))
    holding = db.execute('SELECT 1 FROM scheduled_executions WHERE connection_id=? AND finished_at IS NULL', (row['connection_id'],)).fetchone()
    reset_continuous(db, row['connection_id'], now, bool(holding))


def recover(db, now):
    rows = db.execute('SELECT * FROM scheduled_executions WHERE finished_at IS NULL AND run_id IS NULL').fetchall()
    for row in rows:
        finish(db, row, 'interrupted', 'Server restarted before the scheduled queue could start. Waiting for the next schedule.', now)


def review(db, connection_id, now):
    config = balancing.settings(db, connection_id)
    rows = db.execute('SELECT * FROM scheduled_executions WHERE connection_id=? AND finished_at IS NULL ORDER BY slot',
                      (connection_id,)).fetchall()
    for row in rows:
        if row['run_id']:
            run = migration_queue.document(db, connection_id, row['run_id'])
            if not run or not run['active']:
                finish(db, row, run['status'] if run else 'interrupted', run['message'] if run else 'Scheduled queue is unavailable.', now)
    for kind in ('qemu', 'lxc'):
        schedule = due(db, connection_id, config, now, kind)
        if schedule:
            match = 'LIKE' if kind == 'lxc' else 'NOT LIKE'
            busy = db.execute(f"SELECT 1 FROM scheduled_executions WHERE connection_id=? AND finished_at IS NULL AND schedule_key {match} 'lxc/%'", (connection_id,)).fetchone()
            db.execute('INSERT INTO scheduled_executions VALUES(?,?,?,?,?,?,NULL,?)',
                       (connection_id, schedule['schedule_key'], schedule['slot'], now, 'skipped' if busy else 'waiting',
                        'The previous run of this schedule is still pending.' if busy else f'Waiting to start {"VM + LXC" if kind == "lxc" else "VM"} balancing.', now if busy else None))
            if not busy:
                reset_continuous(db, connection_id, now, True)
    pending = db.execute("SELECT * FROM scheduled_executions WHERE connection_id=? AND run_id IS NULL AND finished_at IS NULL ORDER BY slot",
                         (connection_id,)).fetchall()
    for row in pending:
        kind = 'lxc' if row['schedule_key'].startswith('lxc/') else 'qemu'
        current = planning_schedule.describe(db, connection_id, config, now, guest_kind=kind)
        if config['paused'] or not current['enabled'] or current['schedule_key'] != row['schedule_key']:
            finish(db, row, 'cancelled', 'Scheduled balancing was disabled, paused or rescheduled.', now)
            continue
        backup = backup_guard.read(db, connection_id, now)
        if backup.get('state') == 'backup':
            finish(db, row, 'cancelled_backup', 'Backup detected. This scheduled run is cancelled; the next schedule will reassess.', now)
            continue
        active = migration_queue.active(db, connection_id)
        if active:
            if active['trigger'] == 'continuous':
                migration_queue.stop(db, connection_id, active['id'], now)
                if migration_queue.active(db, connection_id):
                    continue
            elif active['trigger'] == 'scheduled':
                continue
            else:
                finish(db, row, 'skipped', 'Another migration or maintenance queue owns the cluster. Waiting for the next schedule.', now)
                continue
        run_id = str(uuid5(NAMESPACE_URL, f'proxha/scheduled/{connection_id}/{row["schedule_key"]}/{row["slot"]}'))
        try:
            migration_queue.start(db, connection_id, run_id, now, trigger='scheduled',
                                  guest_kind=kind)
        except ProxmoxError as exc:
            finish(db, row, 'skipped', str(exc), now)
            continue
        db.execute("UPDATE scheduled_executions SET status='running',message=?,run_id=? WHERE connection_id=? AND schedule_key=? AND slot=?",
                   ('Scheduled queue started. Continuous balancing is temporarily paused.', run_id,
                    connection_id, row['schedule_key'], row['slot']))
    db.execute('DELETE FROM scheduled_executions WHERE finished_at<?', (now - telemetry.RETENTION,))
