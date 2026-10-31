from datetime import datetime, timezone

from . import telemetry


def automatic(db, connection_id, resource, now):
    rows = [dict(row) for row in db.execute('''
        SELECT s.resource,s.source,s.target,s.score,s.operation_id,s.created_at AS started_at,
               s.verified_at AS finished_at,r.trigger,t.upid
        FROM migration_run_steps s JOIN migration_runs r ON r.id=s.run_id
        JOIN operation_tasks t ON t.operation_id=s.operation_id
        WHERE r.connection_id=? AND s.resource=? AND r.trigger IN ('continuous','scheduled','placement_rules')
        AND r.maintenance_node IS NULL AND t.status='completed'
        AND s.verified_at>=? AND s.verified_at<=?
        ORDER BY finished_at,s.created_at
    ''', (connection_id, resource, now - telemetry.RETENTION, now))]
    days = {}
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index else None
        row['return_move'] = bool(previous and row['finished_at'] - previous['finished_at'] <= 6 * 3600
                                  and row['target'] == previous['source'] and row['source'] == previous['target'])
        day = datetime.fromtimestamp(row['finished_at'], timezone.utc).date().isoformat()
        days[day] = days.get(day, 0) + 1
    return {'resource': resource, 'retention_days': 7, 'migrations_7d': len(rows),
            'migrations_24h': sum(r['finished_at'] >= now - 86400 for r in rows),
            'return_moves_7d': sum(r['return_move'] for r in rows), 'by_day': days,
            'migrations': list(reversed(rows[-50:])),
            'scope': 'Completed automatic migrations only. Manual moves and maintenance are excluded. A return flag marks consecutive automatic moves reversing the same host pair within six hours; review Tasks & Events for intervening manual moves.'}
