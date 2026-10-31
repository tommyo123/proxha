from . import telemetry


def changes(db, connection_id, now, window):
    rows = db.execute('''SELECT source,target,observed_at AS at FROM placement_events
        WHERE connection_id=? AND observed_at>? AND observed_at<=?
        UNION ALL SELECT COALESCE(source,node),target,COALESCE(finished_at,observed_at) FROM native_tasks
        WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND status='completed'
        AND COALESCE(finished_at,observed_at)>? AND COALESCE(finished_at,observed_at)<=?''',
        (connection_id, now - window, now, connection_id, now - window, now))
    result = {}
    for source, target, at in rows:
        for node in (source, target):
            if node:
                result[node] = max(result.get(node, at), at)
    return result


def correct(db, connection_id, host, sample, changed_at, now):
    if changed_at is None:
        return
    rows = db.execute('''SELECT cpu,observed_at FROM metric_observations
        WHERE connection_id=? AND resource=? AND observed_at>? AND observed_at<=?
        AND quality!='unavailable' AND cpu IS NOT NULL ORDER BY observed_at''',
        (connection_id, sample['resource'], changed_at, now)).fetchall()
    last_gap = max((i for i in range(1, len(rows)) if rows[i]['observed_at'] - rows[i - 1]['observed_at'] > 2 * telemetry.INTERVAL), default=0)
    rows = rows[last_gap:]
    ready = (len(rows) >= 4 and rows[-1]['observed_at'] - changed_at >= 60
             and rows[-1]['observed_at'] - rows[0]['observed_at'] >= 45
             and now - rows[-1]['observed_at'] <= 45)
    host.update(cpu_settling=not ready, cpu_history_since=changed_at)
