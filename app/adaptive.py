import math
from collections import defaultdict
from pydantic import BaseModel, ConfigDict, Field

from . import telemetry

DEFAULTS = {'continuous_enabled': False, 'continuous_aggressiveness': 3, 'continuous_interval_seconds': 30,
            'continuous_migrations_per_hour': 10, 'migration_penalty_decay_minutes': 120}
PROFILES = {
    1: {'minimum_score': 12, 'hold_seconds': 600, 'settle_seconds': 600, 'cooldown_seconds': 14400, 'effort': 'conservative'},
    2: {'minimum_score': 8, 'hold_seconds': 300, 'settle_seconds': 300, 'cooldown_seconds': 10800, 'effort': 'conservative'},
    3: {'minimum_score': 5, 'hold_seconds': 180, 'settle_seconds': 180, 'cooldown_seconds': 7200, 'effort': 'balanced'},
    4: {'minimum_score': 2.5, 'hold_seconds': 120, 'settle_seconds': 120, 'cooldown_seconds': 3600, 'effort': 'thorough'},
    5: {'minimum_score': 1, 'hold_seconds': 60, 'settle_seconds': 60, 'cooldown_seconds': 3600, 'effort': 'thorough'},
}
RETURN_WINDOW = 6 * 3600
SWAP_WINDOW = 2 * 3600
# Cooldowns and return protection prevent repeated placement reversals.
SCHEDULED_POLICY = {'cooldown_seconds': 4 * 3600, 'penalty_decay_minutes': 120}
MANUAL_POLICY = {'cooldown_seconds': 3600, 'penalty_decay_minutes': 120}


def churn_policy(continuous, scheduled, continuous_policy=None):
    return continuous_policy if continuous else SCHEDULED_POLICY if scheduled else MANUAL_POLICY


class AggressivenessInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    aggressiveness: int = Field(ge=1, le=5, strict=True)
    revision: int = Field(ge=0, strict=True)


def profile(settings):
    return {**PROFILES[settings['continuous_aggressiveness']], 'return_window_seconds': RETURN_WINDOW,
            'penalty_decay_minutes': settings.get('migration_penalty_decay_minutes', 120)}


def movements(db, connection_id, now):
    result = defaultdict(list)
    events = [dict(r) for r in db.execute('SELECT resource,observed_at AS at,source,target FROM placement_events WHERE connection_id=? AND observed_at>=? ORDER BY observed_at', (connection_id, now - telemetry.RETENTION))]
    events += [dict(r) for r in db.execute("SELECT resource,COALESCE(finished_at,observed_at) AS at,source,target FROM native_tasks WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND status='completed' AND COALESCE(finished_at,observed_at)>=?", (connection_id, now - telemetry.RETENTION))]
    for event in events:
        if not event['resource'] or event['at'] > now:
            continue
        existing = result[event['resource']]
        duplicate = next((e for e in existing if abs(e['at'] - event['at']) <= 180 and all(not e[k] or not event[k] or e[k] == event[k] for k in ('source', 'target'))), None)
        if duplicate:
            for key in ('source', 'target'):
                duplicate[key] = duplicate[key] or event[key]
        else:
            existing.append(event)
    return result


def flows(history):
    # Latest move per directed host pair across all guests.
    result = {}
    for resource, events in history.items():
        for event in events:
            key = (event['source'], event['target'])
            if all(key) and event['at'] > result.get(key, {'at': -1})['at']:
                result[key] = {'at': event['at'], 'resource': resource}
    return result


def movement_cost(events, target, now, policy, source=None, pair_flows=None):
    recent = sum(now - e['at'] < 86400 for e in events)
    cooldown = policy['cooldown_seconds'] * 2 ** min(3, max(0, recent - 1))
    last = max((e['at'] for e in events), default=None)
    decay = policy.get('penalty_decay_minutes', 120)
    penalty = min(100, sum(100 * max(0, 1 - max(0, math.floor((now - e['at']) / 60)) / decay) for e in events))
    reverse = [e for e in events if now - e['at'] < RETURN_WINDOW and target in (e['source'], e['target'])]
    # Any guest's recent move target -> source would be undone by this move.
    swap = (pair_flows or {}).get((target, source)) if source else None
    swap_until = swap['at'] + min(policy['cooldown_seconds'], SWAP_WINDOW) if swap else None
    reason = None
    if last is not None and now - last < cooldown:
        reason = f'Migration cooldown: {math.ceil((last + cooldown - now) / 60)} minutes remain after {recent} moves in 24 hours.'
    elif reverse:
        reason = 'Return protection: this guest visited the target within the last six hours.'
    elif swap_until and now < swap_until:
        reason = (f'Swap protection: {swap["resource"]} moved from {target} to {source} {math.floor((now - swap["at"]) / 60)} minutes ago; '
                  f'moves in the opposite direction wait {math.ceil((swap_until - now) / 60)} more minutes.')
    return {'penalty': round(penalty, 2), 'moves_24h': recent, 'moves_7d': len(events),
            'cooldown_seconds': cooldown, 'last_moved_at': last, 'reason': reason,
            'penalty_decay_minutes': decay, 'penalty_per_minute': round(100 / decay, 3)}


def hourly_budget(db, connection_id, now, limit):
    times = [r[0] for r in db.execute('''SELECT s.created_at FROM migration_run_steps s
        JOIN migration_runs r ON r.id=s.run_id WHERE r.connection_id=? AND r.trigger='continuous'
        AND s.created_at>? AND s.created_at<=? ORDER BY s.created_at''', (connection_id, now - 3600, now))]
    return {'used': len(times), 'limit': limit, 'remaining': max(0, limit - len(times)),
            'retry_at': times[-limit] + 3600 if len(times) >= limit else None}


def longterm_profiles(db, connection_id, now):
    rows = db.execute('''SELECT resource,
        AVG(CASE WHEN sampled_at>=? THEN cpu*cpu_count END) AS cpu_24h,
        AVG(cpu*cpu_count) AS cpu_7d,
        MAX(CASE WHEN sampled_at>=? THEN json_extract(metadata,'$.summary.cpu_busy_cores_max') END) AS cpu_peak_24h,
        AVG(CASE WHEN sampled_at>=? THEN COALESCE(mem_host, CASE WHEN resource LIKE 'lxc/%' THEN mem_used END) END) AS resident_24h,
        MAX(CASE WHEN sampled_at>=? THEN COALESCE(json_extract(metadata,'$.summary.max.mem_host'), CASE WHEN resource LIKE 'lxc/%' THEN json_extract(metadata,'$.summary.max.mem_used') END) END) AS resident_peak_24h,
        COUNT(*) AS minutes FROM metric_samples
        WHERE connection_id=? AND sampled_at>=? AND sampled_at<? AND (resource LIKE 'qemu/%' OR resource LIKE 'lxc/%')
        GROUP BY resource''', (now - 86400, now - 86400, now - 86400, now - 86400, connection_id, now - telemetry.RETENTION, now))
    return {r['resource']: dict(r) for r in rows}
