"""Replay stored observations through the planner to compare settings without migrating anything."""
import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import adaptive, backup_guard, balancing, continuous, demand, telemetry

MODES = {'run_now': {'run_now': True}, 'scheduled': {'run_now': True, 'scheduled': True}, 'continuous': {'continuous': True}}
CLEAR = {'blocked': False, 'state': 'clear', 'message': 'Replay assumes no backups.'}
CONFIG_TABLES = ('balancing_settings', 'host_performance', 'migration_groups', 'migration_group_members',
                 'resource_groups', 'resource_group_members', 'placement_rules', 'placement_rule_groups',
                 'guest_policies', 'guest_storage', 'activity_status', 'infrastructure_sources')
COMPARISON_SECONDS = 90
COMPARISON_EVALUATIONS = 2000


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hours: int = Field(default=24, ge=1, le=24, strict=True)
    step_minutes: int = Field(default=5, ge=1, le=60, strict=True)
    mode: Literal['run_now', 'scheduled', 'continuous'] = 'continuous'
    # Settings to try instead of the saved ones, for example {"distribution_effort": "thorough"}.
    settings: dict = Field(default_factory=dict)


class ComparisonInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hours: int = Field(default=6, ge=1, le=24, strict=True)
    step_minutes: int = Field(default=1, ge=1, le=60, strict=True)
    levels: list[Annotated[int, Field(strict=True, ge=1, le=5)]] = Field(default_factory=lambda: [1, 3, 5], min_length=1, max_length=5)

    @field_validator('levels')
    @classmethod
    def unique_levels(cls, levels):
        if len(set(levels)) != len(levels):
            raise ValueError('Select each aggressiveness level at most once.')
        return sorted(levels)


def step_result(result, at, mode):
    move = next((m for m in result['plan'] if m.get('policy_allowed', True)), None)
    ready = continuous.eligible(result) if mode == 'continuous' else result['proposals']
    known_hosts = [h for h in result['hosts'] if h['state'] not in ('insufficient_data', 'offline', 'maintenance')]
    selected = None
    if move:
        selected = {k: move[k] for k in ('resource', 'source', 'target', 'score')}
        selected['members'] = [{k: m[k] for k in ('resource', 'source', 'target')} for m in move.get('unit_moves', [move])]
        selected['projected_relief_pp'] = 100 * (move['before'] - move['after']) if move.get('before') is not None and move.get('after') is not None else None
        selected['score_breakdown'] = move.get('score_breakdown', {})
    return {'at': at, 'status': result['status'], 'move': selected, 'ready': bool(ready),
            'measured': bool(known_hosts), 'complete_host_data': bool(result['hosts']) and len(known_hosts) == len(result['hosts']),
            'blocked': result['blocked']}


def copy_rows(source, destination, table, where, parameters):
    cursor = source.execute(f'SELECT * FROM {table} WHERE {where}', parameters)
    placeholders = ','.join('?' for _ in cursor.description)
    while rows := cursor.fetchmany(128):
        destination.executemany(f'INSERT OR IGNORE INTO {table} VALUES({placeholders})', rows)


def snapshot(source, connection_id, start, now, lookback):
    # A private temporary file bounds SQLite memory and is deleted when closed.
    if source.in_transaction:
        raise ValueError('Replay needs a database connection without an open transaction.')
    copy = sqlite3.connect('')
    copy.row_factory = sqlite3.Row
    copy.execute('PRAGMA foreign_keys=OFF')
    copy.execute('PRAGMA cache_size=-8192')
    copy.execute('PRAGMA temp_store=FILE')
    source.execute('BEGIN')
    try:
        schema = source.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('table','index') ORDER BY type='index'").fetchall()
        for row in schema:
            copy.execute(row[0])
        for table in CONFIG_TABLES:
            copy_rows(source, copy, table, 'connection_id=?', (connection_id,))
        if lookback is None:
            lookback = max(balancing.WINDOW, balancing.settings(copy, connection_id)['overload_hold_seconds'] + 45)
        copy_rows(source, copy, 'metric_observations', 'connection_id=? AND sampled_at BETWEEN ? AND ?',
                  (connection_id, start - lookback, now))
        copy_rows(source, copy, 'infrastructure_samples', 'connection_id=? AND sampled_at BETWEEN ? AND ?',
                  (connection_id, start - 45, now))
        # Preserve the last known inventory entry even if its telemetry is stale.
        copy_rows(source, copy, 'metric_observations', '''connection_id=? AND (resource,sampled_at) IN (
            SELECT resource,MAX(sampled_at) FROM metric_observations WHERE connection_id=?
            AND sampled_at>=? AND sampled_at<? GROUP BY resource)''',
                  (connection_id, connection_id, start - telemetry.RAW_RETENTION, start - lookback))
        for table, timestamp in (('placement_events', 'observed_at'), ('migration_failures', 'occurred_at')):
            copy_rows(source, copy, table, f'connection_id=? AND {timestamp} BETWEEN ? AND ?',
                      (connection_id, start - telemetry.RETENTION, now))
        copy_rows(source, copy, 'native_tasks', "connection_id=? AND status='completed' AND COALESCE(finished_at,observed_at) BETWEEN ? AND ?",
                  (connection_id, start - telemetry.RETENTION, now))
        copy.commit()
        return copy
    except BaseException:
        copy.close()
        raise
    finally:
        source.rollback()


def run(source, connection_id, data, now=None):
    now = time.time() if now is None else now
    if source.in_transaction:
        raise ValueError('Replay needs a database connection without an open transaction.')
    config = balancing.settings(source, connection_id)
    revision = config.pop('revision')
    candidate = balancing.SettingsInput(**{**config, **data.settings, 'revision': revision})
    db = snapshot(source, connection_id, now - data.hours * 3600, now,
                  max(balancing.WINDOW, candidate.overload_hold_seconds + 45))
    try:
        db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)',
                   (connection_id, candidate.model_dump_json(exclude={'revision'}), revision))
        first = db.execute('SELECT MIN(sampled_at) FROM metric_observations WHERE connection_id=?', (connection_id,)).fetchone()[0]
        start = max(now - data.hours * 3600, first + demand.SMOOTH_SECONDS if first is not None else now)
        steps, previous = [], None
        at = start
        while at <= now:
            db.execute('UPDATE guest_storage SET checked_at=? WHERE connection_id=?', (at, connection_id))
            backup_guard.save(db, connection_id, CLEAR, at)
            result = balancing.evaluate(db, connection_id, at, previous, manual=False, **MODES[data.mode])
            previous = result
            steps.append(step_result(result, at, data.mode))
            at += data.step_minutes * 60
        return summarize(steps, data)
    finally:
        db.close()


def summarize(steps, data):
    moves = [s['move'] for s in steps if s['move']]
    reversals, last_flow = 0, {}
    for move in moves:
        pair = tuple(sorted((move['source'], move['target'])))
        if last_flow.get(pair) not in (None, move['source']):
            reversals += 1
        last_flow[pair] = move['source']
    changes = sum(1 for a, b in zip(moves, moves[1:]) if (a['resource'], a['target']) != (b['resource'], b['target']))
    counted = Counter((m['resource'], m['source'], m['target']) for m in moves)
    returns, last_guest, members, blocks = 0, {}, set(), Counter()
    for step in steps:
        for block in step.get('blocked', []):
            blocks[block['reason']] += block['count']
        if not step['move']:
            continue
        for member in step['move'].get('members', [step['move']]):
            resource = member['resource']
            members.add(resource)
            previous = last_guest.get(resource)
            if previous and step['at'] - previous['at'] <= adaptive.RETURN_WINDOW and member['source'] == previous['target'] and member['target'] == previous['source']:
                returns += 1
            last_guest[resource] = {**member, 'at': step['at']}
    readiness = data.mode == 'scheduled' or data.step_minutes * 60 <= (60 if data.mode == 'continuous' else balancing.INTERVAL + 60)
    breakdown_keys = sorted({k for move in moves for k, v in move.get('score_breakdown', {}).items()
                             if isinstance(v, (int, float)) and k not in ('effort_multiplier', 'stickiness_multiplier', 'guest_cpu_psi')})
    relief = [m['projected_relief_pp'] for m in moves if m.get('projected_relief_pp') is not None]
    return {'mode': data.mode, 'hours': data.hours, 'step_minutes': data.step_minutes, 'settings': data.settings,
            'evaluations': len(steps), 'with_move': len(moves), 'distinct_moves': len(counted),
            'recommendation_changes': changes, 'direction_reversals': reversals,
            'guest_reverse_recommendations': returns, 'guests': sorted(members),
            'ready_evaluations': sum(s.get('ready', False) for s in steps) if readiness else None,
            'readiness_assessed': readiness,
            'measured_evaluations': sum(s.get('measured', False) for s in steps),
            'complete_host_data_evaluations': sum(s.get('complete_host_data', False) for s in steps),
            'started_at': steps[0]['at'] if steps else None, 'ended_at': steps[-1]['at'] if steps else None,
            'score_median': median([m['score'] for m in moves]) if moves else None,
            'projected_relief_mean_pp': round(sum(relief) / len(relief), 2) if relief else None,
            'score_breakdown_mean': {k: round(sum(m.get('score_breakdown', {}).get(k, 0) for m in moves) / len(moves), 2) for k in breakdown_keys},
            'blocked_reasons': [{'reason': reason, 'occurrences': count} for reason, count in blocks.most_common(8)],
            'top_moves': [{'resource': r, 'source': s, 'target': t, 'evaluations': n} for (r, s, t), n in counted.most_common(10)],
            'statuses': dict(Counter(s['status'] for s in steps)),
            'note': 'Each step evaluates the recorded state; recommended moves are not applied, so a persistent imbalance repeats '
                    'the same recommendation. Host-pair direction reversals can involve different guests. Same-guest reverse suggestions '
                    'count opposite recommendations within six hours, not completed migrations. Current settings, rules, storage and HA policy are used. '
                    'Live queues, tasks, maintenance and pending repairs are excluded. Completed migration history is evaluated as of each step. '
                    'Backups are assumed clear and storage checks current. Readiness checks planner gates only; execution preflight, '
                    'hourly execution limits and settling after hypothetical moves are not simulated. Steps longer than one minute cannot assess continuous readiness.'}


def compare(source, connection_id, data, now=None):
    now = time.time() if now is None else now
    deadline = time.monotonic() + COMPARISON_SECONDS
    if source.in_transaction:
        raise ValueError('Replay needs a database connection without an open transaction.')
    # Read settings from the same snapshot as observations so every profile has identical input.
    db = snapshot(source, connection_id, now - data.hours * 3600, now, None)
    try:
        config = balancing.settings(db, connection_id)
        saved_level = config['continuous_aggressiveness']
        levels = sorted(set(data.levels) | {saved_level})
        candidates = {level: balancing.SettingsInput(**{**config, 'continuous_aggressiveness': level}) for level in levels}
        histories, previous = {level: [] for level in levels}, dict.fromkeys(levels)
        first = db.execute('SELECT MIN(sampled_at) FROM metric_observations WHERE connection_id=?', (connection_id,)).fetchone()[0]
        start = max(now - data.hours * 3600, first + demand.SMOOTH_SECONDS if first is not None else now)
        at, limited = start, None
        while first is not None and at <= now:
            if time.monotonic() >= deadline or sum(map(len, histories.values())) + len(levels) > COMPARISON_EVALUATIONS:
                limited = 'time_budget' if time.monotonic() >= deadline else 'evaluation_budget'
                break
            for level in levels:
                db.execute('SAVEPOINT profile')
                try:
                    db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)',
                               (connection_id, candidates[level].model_dump_json(exclude={'revision'}), config['revision']))
                    db.execute('UPDATE guest_storage SET checked_at=? WHERE connection_id=?', (at, connection_id))
                    backup_guard.save(db, connection_id, CLEAR, at)
                    result = balancing.evaluate(db, connection_id, at, previous[level], manual=False, continuous=True)
                    previous[level] = continuous.observation(result)
                    histories[level].append(step_result(result, at, 'continuous'))
                finally:
                    db.execute('ROLLBACK TO profile')
                    db.execute('RELEASE profile')
            at += data.step_minutes * 60
        results = []
        for level in levels:
            summary = summarize(histories[level], ReplayInput(hours=data.hours, step_minutes=data.step_minutes, settings={'continuous_aggressiveness': level}))
            results.append({**summary, 'level': level, 'saved': level == saved_level, 'policy': adaptive.profile(candidates[level].model_dump())})
        baseline = next(r for r in results if r['saved'])
        for result in results:
            result['delta'] = {k: result[k] - baseline[k] if result[k] is not None and baseline[k] is not None else None
                               for k in ('with_move', 'ready_evaluations', 'guest_reverse_recommendations', 'score_median')}
        return {'profiles': results, 'saved_level': saved_level, 'settings_revision': config['revision'],
                'requested_start': now - data.hours * 3600, 'requested_end': now, 'started_at': baseline['started_at'], 'ended_at': baseline['ended_at'],
                'partial': limited is not None, 'limit_reason': limited, 'has_observations': first is not None,
                'readiness_assessed': data.step_minutes <= 1, 'snapshot_shared': True,
                'note': 'All profiles use one recorded snapshot and the same evaluation timestamps. Only continuous aggressiveness changes; '
                        'the saved level is included as the reference. Results compare recommendations, not simulated executions or predicted migration counts. '
                        'No settings or live cluster state are changed. A partial run compares only the displayed interval.'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Replay recorded observations through the ProxHA planner.')
    parser.add_argument('--database', default=str(database_path()))
    parser.add_argument('--connection', type=int, default=1)
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--step-minutes', type=int, default=5)
    parser.add_argument('--mode', choices=sorted(MODES), default='continuous')
    parser.add_argument('--compare-levels', help='Compare continuous levels, e.g. 1,3,5. The saved level is always included.')
    parser.add_argument('--set', action='append', default=[], metavar='NAME=JSON', help='Override a setting, e.g. --set distribution_effort=\'"thorough"\'')
    args = parser.parse_args()
    if args.compare_levels and (args.set or args.mode != 'continuous'):
        parser.error('--compare-levels uses saved continuous settings; --set and other modes are single-profile options.')
    overrides = {name: json.loads(value) for name, value in (item.split('=', 1) for item in args.set)}
    source = sqlite3.connect(Path(args.database).resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    try:
        result = compare(source, args.connection, ComparisonInput(hours=args.hours, step_minutes=args.step_minutes, levels=[int(v) for v in args.compare_levels.split(',')])) if args.compare_levels else run(
            source, args.connection, ReplayInput(hours=args.hours, step_minutes=args.step_minutes, mode=args.mode, settings=overrides))
        print(json.dumps(result, indent=2))
    finally:
        source.close()


def database_path():
    from .main import DATA
    return DATA / 'proxha.sqlite3'


if __name__ == '__main__':
    main()
