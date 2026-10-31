from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULTS = {'distribution_enabled': False, 'distribution_schedule': 'daily',
            'distribution_time': '03:00', 'distribution_interval_hours': 1,
            'distribution_weekly': [], 'timezone': 'Europe/Oslo',
            'container_schedule': {'enabled': False, 'cadence': 'daily', 'time': '04:00',
                                   'interval_hours': 1, 'weekly': [], 'timezone': 'Europe/Oslo'}}
WINDOW = 900
SCHEMA = '''
CREATE TABLE IF NOT EXISTS balancing_schedule_state (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    schedule_key TEXT NOT NULL, slot REAL NOT NULL, evaluated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS balancing_lxc_schedule_state (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    schedule_key TEXT NOT NULL, slot REAL NOT NULL, evaluated_at REAL NOT NULL
);
'''


def describe(db, connection_id, configuration, now, manual=False, run_now=False, guest_kind='qemu'):
    if guest_kind == 'lxc':
        values = {**DEFAULTS['container_schedule'], **configuration.get('container_schedule', {})}
        configuration = {f'distribution_{key}': values[field] for key, field in
                         (('enabled', 'enabled'), ('schedule', 'cadence'), ('time', 'time'),
                          ('interval_hours', 'interval_hours'), ('weekly', 'weekly'))} | {'timezone': values['timezone']}
    zone = ZoneInfo(configuration['timezone'])
    local = datetime.fromtimestamp(now, zone)
    cadence = configuration['distribution_schedule']
    if cadence == 'hourly':
        interval = configuration['distribution_interval_hours'] * 3600
        slot = int(now // interval) * interval
        upcoming = [slot + interval * index for index in range(1, 4)]
    else:
        weekly = {entry['day']: entry['time'] for entry in configuration['distribution_weekly']}
        slots = []
        for offset in range(-8, 23):
            day = local.date() + timedelta(days=offset)
            clock = configuration['distribution_time'] if cadence == 'daily' else weekly.get(day.weekday())
            if clock is None:
                continue
            hour, minute = map(int, clock.split(':'))
            slots.append(datetime.combine(day, time(hour, minute), zone).timestamp())
        slot = max((value for value in slots if value <= now), default=None)
        upcoming = sorted(value for value in slots if value > now)[:3]
    detail = str(configuration['distribution_interval_hours']) if cadence == 'hourly' else configuration['distribution_time'] if cadence == 'daily' else ','.join(f"{entry['day']}={entry['time']}" for entry in sorted(configuration['distribution_weekly'], key=lambda e: e['day']))
    key = f"{cadence}/{detail}/{configuration['timezone']}"
    table = 'balancing_lxc_schedule_state' if guest_kind == 'lxc' else 'balancing_schedule_state'
    if guest_kind == 'lxc':
        key = 'lxc/' + key
    row = db.execute(f'SELECT * FROM {table} WHERE connection_id=?', (connection_id,)).fetchone()
    already_seen = row and row['schedule_key'] == key and row['slot'] == slot
    enabled = configuration['distribution_enabled']
    in_window = slot is not None and 0 <= now - slot < WINDOW
    return {'guest_kind': guest_kind, 'enabled': enabled, 'cadence': cadence, 'time': configuration['distribution_time'],
            'interval_hours': configuration['distribution_interval_hours'], 'weekly': configuration['distribution_weekly'],
            'timezone': configuration['timezone'], 'next_at': int(upcoming[0]) if enabled and upcoming else None,
            'upcoming': [int(value) for value in upcoming] if enabled else [],
            'slot': int(slot) if slot is not None else None, 'schedule_key': key,
            'window_end': int(slot + WINDOW) if slot is not None else None,
            'active': bool(run_now or enabled and (manual or in_window)), 'manual_preview': manual or run_now,
            'one_off': run_now,
            'new_run': bool(enabled and in_window and not already_seen and not manual and not run_now),
            'last_evaluated_at': row['evaluated_at'] if row and row['schedule_key'] == key else None}


def record(db, connection_id, schedule, now):
    if schedule['new_run']:
        table = 'balancing_lxc_schedule_state' if schedule.get('guest_kind') == 'lxc' else 'balancing_schedule_state'
        db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?,?)',
                   (connection_id, schedule['schedule_key'], schedule['slot'], now))
