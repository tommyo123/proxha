import math

from . import telemetry


WINDOW = 300
SETTLE = 60
METRICS = {'cpu': ('CPU usage', '%', 100), 'mem_used': ('Memory used', 'GiB', 1 / 1024 ** 3),
           'pressurecpusome': ('CPU PSI some', '%', 1), 'pressureiosome': ('I/O PSI some', '%', 1),
           'pressurememorysome': ('Memory PSI some', '%', 1),
           'balloon_reclaimed': ('Ballooned memory', 'GiB', 1 / 1024 ** 3)}


def window(db, connection_id, resource, node, start, now):
    rows = db.execute('''SELECT * FROM metric_samples WHERE connection_id=? AND resource=?
        AND sampled_at>=? AND sampled_at<? AND sampled_at+60<=? ORDER BY sampled_at''',
        (connection_id, resource, start, start + WINDOW, now)).fetchall()
    samples = [telemetry.sample_dict(r) for r in rows]
    values = {}
    for key in METRICS:
        valid = []
        for sample in samples:
            summary = sample['metadata'].get('summary', {})
            count = summary.get('counts', {}).get(key, 0)
            nodes = summary.get('nodes', [])
            if (sample['node'] == node and nodes == [node] and sample['quality'] != 'unavailable'
                    and summary.get('statuses') == (['online'] if resource.startswith('node/') else ['running'])
                    and sample[key] is not None and 3 <= count <= 4):
                valid.append((sample[key], count))
        count = sum(n for _, n in valid)
        values[key] = {'value': sum(v * n for v, n in valid) / count if len(valid) >= 4 else None,
                       'coverage': count / 20, 'minutes': len(valid)}
    return values


def report(db, connection_id, resource, operation_id, now):
    row = db.execute('''SELECT s.*,r.trigger,r.finished_at AS run_finished_at,t.upid FROM migration_run_steps s
        JOIN migration_runs r ON r.id=s.run_id JOIN operation_tasks t ON t.operation_id=s.operation_id
        WHERE r.connection_id=? AND s.resource=? AND s.operation_id=? AND t.status='completed'
        AND r.trigger IN ('continuous','scheduled','placement_rules') AND r.maintenance_node IS NULL
        AND s.verified_at BETWEEN ? AND ?''', (connection_id, resource, operation_id, now - telemetry.RETENTION, now)).fetchone()
    if not row:
        return None
    cohort = [dict(s) for s in db.execute('''SELECT s.resource,s.operation_id,s.created_at,s.verified_at,s.settled_at,s.source,s.target
        FROM migration_run_steps s JOIN operation_tasks t ON t.operation_id=s.operation_id
        WHERE s.run_id=? AND (s.operation_id=? OR (t.upid=? AND t.upid IS NOT NULL))''',
        (row['run_id'], operation_id, row['upid']))]
    # Members still migrating keep the report pending. Failed or unknown members settle without
    # verification; a finished (interrupted) run no longer tracks its unsettled members.
    run_ended = row['run_finished_at'] is not None
    in_flight = any(s['verified_at'] is None and s['settled_at'] is None and not run_ended for s in cohort)
    unverified = [s['resource'] for s in cohort if s['verified_at'] is None and (s['settled_at'] is not None or run_ended)]
    before_end = int(min(s['created_at'] for s in cohort) // 60) * 60
    after_start = math.ceil((max(s['verified_at'] or s['settled_at'] or row['verified_at'] for s in cohort) + SETTLE) / 60) * 60
    end = after_start + WINDOW
    result = {'resource': resource, 'source': row['source'], 'target': row['target'], 'score': row['score'],
              'operation_id': operation_id, 'duration_seconds': row['verified_at'] - row['created_at'],
              'status': 'pending', 'available_at': end, 'comparisons': [], 'overlap_detected': False,
              'cohort_members': [s['resource'] for s in cohort],
              'before_window': [before_end - WINDOW, before_end], 'after_window': [after_start, end],
              'message': 'Waiting for five complete post-migration minutes after at least one minute of settling.',
              'scope': 'Observed before/after change, not proof of causation. Migration score is priority, not a predicted percentage improvement. Duration includes placement verification, not measured downtime. Host results include the whole migration group.'}
    if now < end or in_flight:
        return result
    own = {s['operation_id'] for s in cohort}
    overlapping = db.execute('''SELECT s.operation_id FROM migration_run_steps s JOIN migration_runs r ON r.id=s.run_id
        WHERE r.connection_id=? AND s.created_at<? AND COALESCE(s.verified_at,s.settled_at,?)>=?
        AND (s.source IN (?,?) OR s.target IN (?,?))''',
        (connection_id, end, now, before_end - WINDOW, row['source'], row['target'], row['source'], row['target'])).fetchall()
    overlap_count = sum(s['operation_id'] not in own for s in overlapping)
    events = db.execute('''SELECT resource,observed_at,source,target FROM placement_events
        WHERE connection_id=? AND observed_at>=? AND observed_at<? AND (source IN (?,?) OR target IN (?,?))''',
        (connection_id, before_end - WINDOW, end, row['source'], row['target'], row['source'], row['target'])).fetchall()
    # Only verified members can claim their own events and tasks; an unverified member's placement is uncertain.
    members = {s['resource']: s for s in cohort if s['verified_at'] is not None}
    overlap_count += len(unverified)
    for event in events:
        member = members.get(event['resource'])
        if not (member and member['created_at'] <= event['observed_at'] <= member['verified_at'] + 60
                and event['source'] == member['source'] and event['target'] == member['target']):
            overlap_count += 1
    native = db.execute('''SELECT upid,resource,source,target,started_at,finished_at FROM native_tasks WHERE connection_id=?
        AND kind IN ('qmigrate','vzmigrate') AND started_at<? AND COALESCE(finished_at,?)>=?
        AND (source IN (?,?) OR target IN (?,?))''',
        (connection_id, end, now, before_end - WINDOW, row['source'], row['target'], row['source'], row['target'])).fetchall()
    for task in native:
        member = members.get(task['resource'])
        own_native = (member and task['source'] == member['source'] and task['target'] in (None, member['target'])
                      and task['started_at'] >= math.floor(member['created_at']) and task['finished_at'] is not None
                      and task['finished_at'] <= member['verified_at'] + 60)
        if task['upid'] != row['upid'] and not own_native:
            overlap_count += 1
    for entity, key, source, target in (('Source host', f'node/{row["source"]}', row['source'], row['source']),
                                      ('Target host', f'node/{row["target"]}', row['target'], row['target']),
                                      ('Guest', resource, row['source'], row['target'])):
        before = window(db, connection_id, key, source, before_end - WINDOW, now)
        after = window(db, connection_id, key, target, after_start, now)
        for metric, (label, unit, scale) in METRICS.items():
            if metric == 'balloon_reclaimed' and entity != 'Guest':
                continue
            a, b = before[metric]['value'], after[metric]['value']
            result['comparisons'].append({'entity': entity, 'metric': label, 'unit': unit,
                'before': a * scale if a is not None else None, 'after': b * scale if b is not None else None,
                'change': (b - a) * scale if a is not None and b is not None else None,
                'before_coverage': before[metric]['coverage'], 'after_coverage': after[metric]['coverage']})
    known = all(c['change'] is not None for c in result['comparisons'] if c['metric'] in ('CPU usage', 'Memory used'))
    result.update(overlap_detected=bool(overlap_count),
                  status='confounded' if overlap_count else 'measured' if known else 'insufficient_data',
                  message=f'Migration group members without a verified move: {", ".join(unverified)}. Their placement cannot be separated from this move; do not attribute the change to it.' if unverified else
                          'Other placement activity overlaps these windows; do not attribute the change to this move.' if overlap_count else
                          'Before/after measurements are available. Workload changes can also affect the result.' if known else
                          'Insufficient comparable CPU/RAM data. Each value requires at least four minutes with three observations per minute.')
    return result
