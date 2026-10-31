import hashlib
import json
import logging
import math
import re
from collections import Counter
from copy import deepcopy
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import infrastructure, adaptive, backup_guard, ha_guests, native_ha, balloon, demand as cpu_demand, distribution, guest_policy, migration_failures, migration_groups, node_maintenance, placement_load, placement_rules, preflight_failures, resource_groups, planning_schedule, pressure_policy, storage, telemetry

INTERVAL = 60
WINDOW = 900
VERSION = 27
GIB = 1024 ** 3
DEFAULTS = {'paused': False, 'container_balancing_enabled': True, 'balloon_recovery_enabled': True, 'delay_seconds': 120, 'hold_seconds': 300, 'maintenance_concurrency': 3, 'placement_concurrency': 2, 'placement_repair_enabled': True, 'maintenance_memory_mode': 'observed', 'distribution_effort': 'balanced', 'scheduled_minimum_score': .5, 'migration_bandwidth_limit_mib': 0, 'ha_guests_enabled': False, 'migration_target_concurrency': 0, **planning_schedule.DEFAULTS, **adaptive.DEFAULTS, **pressure_policy.DEFAULTS}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS host_performance (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    node TEXT NOT NULL, factor REAL NOT NULL, PRIMARY KEY(connection_id,node)
);
CREATE TABLE IF NOT EXISTS balancing_evaluations (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    evaluated_at INTEGER NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(connection_id, evaluated_at)
);
CREATE INDEX IF NOT EXISTS balancing_evaluations_expiry ON balancing_evaluations(evaluated_at);
CREATE TABLE IF NOT EXISTS balancing_settings (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    settings TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
);
''' + resource_groups.SCHEMA + placement_rules.SCHEMA + planning_schedule.SCHEMA + storage.SCHEMA + node_maintenance.SCHEMA + backup_guard.SCHEMA + migration_failures.SCHEMA + preflight_failures.SCHEMA + infrastructure.SCHEMA


class WeeklyReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    day: int = Field(ge=0, le=6, strict=True)
    time: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')


class ContainerSchedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=False, strict=True)
    cadence: Literal['daily', 'hourly', 'weekly'] = 'daily'
    time: str = Field(default='04:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    interval_hours: int = Field(default=1, ge=1, le=168, strict=True)
    weekly: list[WeeklyReview] = Field(default_factory=list, max_length=7)
    timezone: str = Field(default='Europe/Oslo', min_length=1, max_length=64)

    @model_validator(mode='after')
    def valid_schedule(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Use a valid IANA time zone for the LXC schedule.') from None
        days = [entry.day for entry in self.weekly]
        if len(set(days)) != len(days) or self.enabled and self.cadence == 'weekly' and not days:
            raise ValueError('Select unique weekdays and at least one day for an enabled weekly LXC schedule.')
        self.weekly.sort(key=lambda entry: entry.day)
        return self


class PerformanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    factors: dict[str, float] = Field(max_length=64)

    @field_validator('factors')
    @classmethod
    def valid_factors(cls, value):
        for node, factor in value.items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,62}', node) or not .25 <= factor <= 4:
                raise ValueError('Use host names and relative CPU performance factors from 0.25 to 4.')
        return value


def performance(db, connection_id):
    return {r['node']: r['factor'] for r in db.execute('SELECT node,factor FROM host_performance WHERE connection_id=?', (connection_id,))}


def save_performance(db, connection_id, factors):
    db.execute('DELETE FROM host_performance WHERE connection_id=?', (connection_id,))
    # A factor of 1 is the reference and needs no row.
    db.executemany('INSERT INTO host_performance VALUES(?,?,?)', [(connection_id, n, f) for n, f in factors.items() if f != 1])
    return performance(db, connection_id)


def cpu_scale(origin, destination):
    # Busy cores on the source become this many cores on a target with different per-thread performance.
    return origin.get('performance', 1) / destination.get('performance', 1)


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    paused: bool = Field(strict=True)
    balloon_recovery_enabled: bool = Field(default=True, strict=True)
    container_balancing_enabled: bool = Field(default=True, strict=True)
    delay_seconds: int = Field(ge=30, le=1800, strict=True)
    hold_seconds: int = Field(ge=60, le=1800, strict=True)
    revision: int = Field(ge=0, strict=True)
    placement_repair_enabled: bool = Field(default=True, strict=True)
    placement_concurrency: int = Field(default=2, ge=2, le=10, strict=True)
    maintenance_concurrency: int = Field(default=3, ge=1, le=10, strict=True)
    migration_bandwidth_limit_mib: int = Field(default=0, ge=0, le=100000, strict=True)
    ha_guests_enabled: bool = Field(default=False, strict=True)
    migration_target_concurrency: int = Field(default=0, ge=0, le=10, strict=True)
    maintenance_memory_mode: Literal['observed', 'configured'] = 'observed'
    distribution_effort: Literal['conservative', 'balanced', 'thorough'] = 'balanced'
    scheduled_minimum_score: float = Field(default=.5, ge=.1, le=100, strict=True)
    continuous_enabled: bool = Field(default=False, strict=True)
    continuous_aggressiveness: int = Field(default=3, ge=1, le=5, strict=True)
    continuous_interval_seconds: int = Field(default=30, ge=15, le=30, strict=True)
    cpu_threshold_percent: int = Field(default=80, ge=50, le=95, strict=True)
    memory_threshold_percent: int = Field(default=80, ge=50, le=95, strict=True)
    overload_hold_seconds: int = Field(default=300, ge=60, le=1800, strict=True)
    cpu_psi_threshold_percent: int = Field(default=5, ge=1, le=50, strict=True)
    continuous_migrations_per_hour: int = Field(default=10, ge=1, le=1000, strict=True)
    migration_penalty_decay_minutes: int = Field(default=120, ge=15, le=1440, strict=True)
    distribution_enabled: bool = Field(default=False, strict=True)
    container_schedule: ContainerSchedule = Field(default_factory=ContainerSchedule)
    distribution_schedule: Literal['daily', 'hourly', 'weekly'] = 'daily'
    distribution_time: str = Field(default='03:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    distribution_interval_hours: int = Field(default=1, ge=1, le=168, strict=True)
    distribution_weekly: list[WeeklyReview] = Field(default_factory=list, max_length=7)
    timezone: str = Field(default='Europe/Oslo', min_length=1, max_length=64)

    @field_validator('continuous_interval_seconds')
    @classmethod
    def valid_continuous_interval(cls, value):
        if value not in (15, 30):
            raise ValueError('Choose a continuous check interval of 15 or 30 seconds.')
        return value

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Use a valid IANA time zone, for example Europe/Oslo.') from None
        return value

    @model_validator(mode='after')
    def valid_schedule(self):
        days = [entry.day for entry in self.distribution_weekly]
        if len(set(days)) != len(days):
            raise ValueError('Choose each weekday only once.')
        if self.distribution_enabled and self.distribution_schedule == 'weekly' and not days:
            raise ValueError('Select at least one weekday and time for perfect balancing.')
        self.distribution_weekly.sort(key=lambda entry: entry.day)
        return self


def settings(db, connection_id):
    row = db.execute('SELECT * FROM balancing_settings WHERE connection_id=?', (connection_id,)).fetchone()
    return {**DEFAULTS, **(json.loads(row['settings']) if row else {}), 'revision': row['revision'] if row else 0}


def prune(db, now):
    migration_failures.prune(db, now)
    preflight_failures.prune(db, now)
    placement_rules.prune(db, now)
    db.execute('DELETE FROM balancing_evaluations WHERE evaluated_at<?', (int(now) - telemetry.RETENTION,))


def latest(db, connection_id):
    row = db.execute('SELECT result FROM balancing_evaluations WHERE connection_id=? ORDER BY evaluated_at DESC LIMIT 1',
                     (connection_id,)).fetchone()
    return json.loads(row['result']) if row else None


def measured_demand(sample, rows, now, host_busy=None):
    complete = (all(sample.get(k) is not None for k in ('cpu', 'mem_used', 'mem_total', 'cpu_count'))
                and sample['mem_total'] > 0 and sample['cpu_count'] > 0)
    busy = peak = psi = None
    if complete and sample['resource'].startswith('node/'):
        busy = peak = host_busy if host_busy is not None else sample['cpu'] * sample['cpu_count']
    elif complete:
        busy, peak, psi = cpu_demand.guest(sample, rows, now)
    return {'coverage': int(bool(complete)), 'cpu': busy / sample['cpu_count'] if complete else sample['cpu'],
            'busy_cores': busy, 'peak_cores': peak, 'cpu_psi': psi, 'cpu_limit': (sample.get('metadata') or {}).get('cpu_limit'),
            'memory': sample['mem_used'] / sample['mem_total'] if complete else None,
            'nodes': {sample['node']}, 'recent': []}


def demand_profiles(db, connection_id, samples, now):
    # CPU uses placement-adjusted means and guest peaks; RAM uses current measurements.
    history = cpu_demand.window(db, connection_id, now)
    profiles = {r: measured_demand(s, history.get(r, []), now) for r, s in samples.items() if not r.startswith('node/')}
    placements = {r: s['node'] for r, s in samples.items() if r.startswith(('qemu/', 'lxc/')) and s['status'] == 'running'}
    busy_now = {r: profiles[r]['busy_cores'] for r in placements if profiles[r]['busy_cores'] is not None}
    host_busy = cpu_demand.hosts(history, placements, busy_now, now)
    for resource, sample in samples.items():
        if resource.startswith('node/'):
            profiles[resource] = measured_demand(sample, (), now, host_busy.get(sample['node']))
    return profiles


def fresh(sample, now):
    return bool(sample and 0 <= now - sample['observed_at'] <= 45 and sample['quality'] != 'unavailable')


def risk(cpu, memory):
    return .55 * (max(0, cpu) / .8) ** 4 + .45 * (max(0, memory) / .85) ** 6


def pressure_evidence(sample):
    cpu = sample['cpu']
    memory = sample['mem_used'] / sample['mem_total'] if sample['mem_total'] and sample['mem_used'] is not None else None
    if memory is not None and memory >= .95 and (sample['pressurememorysome'] or 0) >= 1:
        return 'RAM is at least 95% and memory PSI some is at least 1%.'
    if cpu is not None and cpu >= .95 and (sample['pressurecpusome'] or 0) >= 20:
        return 'CPU is at least 95% and CPU PSI some is at least 20%.'
    if (sample['pressureiosome'] or 0) >= 10 and (sample['iowait'] or 0) >= .1:
        return 'I/O PSI some is at least 10% and I/O wait is at least 10%; storage review is required.'
    return None


def confirmed_pressure(db, connection_id, resource, now):
    rows = [dict(row) for row in db.execute(
        'SELECT * FROM metric_observations WHERE connection_id=? AND resource=? AND sampled_at>=? AND sampled_at<=? ORDER BY sampled_at',
        (connection_id, resource, now - 90, now))]
    if len(rows) < 5 or not fresh(rows[-1], now):
        return None
    tail = rows[-5:]
    if tail[-1]['observed_at'] - tail[0]['observed_at'] < 55 or any(b['observed_at'] - a['observed_at'] > 25 for a, b in zip(tail, tail[1:])):
        return None
    reasons = [pressure_evidence(row) if row['quality'] != 'unavailable' and row['source_at'] is not None and 0 <= now - row['source_at'] <= 180 else None for row in tail]
    return reasons[-1] if reasons[-1] and all(reason == reasons[-1] for reason in reasons) else None


def model(sample, demand):
    return {'cpu': demand['cpu'], 'cpu_live': sample['cpu'], 'memory': sample['mem_used'] / sample['mem_total'],
            'cpu_recent': demand['cpu'], 'memory_recent': sample['mem_used'] / sample['mem_total'],
            'cpu_pressure': sample['pressurecpusome'], 'memory_pressure': sample['pressurememorysome'],
            'io_pressure': sample['pressureiosome'], 'io_wait': sample['iowait'],
            'cores': sample['cpu_count'], 'ram': sample['mem_total'],
            'peak_cpu': max(sample['cpu'], demand['cpu']), 'metrics_observed_at': sample['observed_at']}


def memory_commitments(samples, node, now):
    guests = [s for s in samples.values() if s['node'] == node and s['resource'].startswith(('qemu/', 'lxc/')) and s['status'] == 'running']
    complete = all(fresh(s, now) and s['mem_total'] is not None for s in guests)
    vms = [s for s in guests if s['resource'].startswith('qemu/')]
    reporting = [s for s in vms if fresh(s, now) and s['balloon_reclaimed'] is not None]
    return {'assigned_memory': sum(s['mem_total'] for s in guests) if complete else None,
            'balloon_reclaimed': sum(s['balloon_reclaimed'] for s in reporting) if reporting or not vms else None,
            'guest_disk_bps': sum((s['disk_read_bps'] or 0) + (s['disk_write_bps'] or 0) for s in guests if fresh(s, now)),
            'balloon_coverage': len(reporting) / len(vms) if vms else 1, 'running_guests': len(guests)}


def dynamic_reasons(host, projected=False):
    reasons = []
    relief = host.get('relief')
    if relief:
        if relief['cpu_ready'] and host['cpu'] >= (relief['cpu_threshold_percent'] - 5) / 100:
            reasons.append('Sustained CPU overload with multiple heavy guests or CPU PSI contention.')
        if relief['memory_ready'] and host['memory'] >= (relief['memory_threshold_percent'] - 5) / 100:
            reasons.append('Sustained memory overload; prefer less active guests with useful resident RAM relief.')
    elif host['cpu'] >= .8 or host['memory'] >= .8:
        reasons.append('Host CPU or RAM exceeds the overload threshold.')
    if projected:
        return reasons
    if host.get('contended_guests'):
        reasons.append(f'{len(host["contended_guests"])} guests wait for host CPU: their CPU PSI some is at least {cpu_demand.CONTENTION_PSI}%.')
    if (host['cpu_pressure'] or 0) >= 20:
        reasons.append('CPU PSI some is at least 20%; workloads are waiting for CPU time.')
    if (host['memory_pressure'] or 0) >= 1:
        reasons.append('Memory PSI some is at least 1%; memory stalls require relief even below the RAM threshold.')
    if (host['io_pressure'] or 0) >= 5 and (host['io_wait'] or 0) >= .05:
        reasons.append('I/O pressure and I/O wait indicate impaired response; storage review is required.')
    return reasons


def placement_reasons(source, models, continuing=False, projected=False, effort='conservative'):
    origin = models[source]
    reasons = dynamic_reasons(origin, projected=projected) + distribution.reasons(origin, effort) + balloon.reasons(origin)
    cpu_gap, ram_gap = (.15, .10) if continuing else (.25, .20)
    for name, target in models.items():
        if name == source:
            continue
        if origin.get('migration_group_id') is not None and target.get('migration_group_id') is not None and origin['migration_group_id'] != target['migration_group_id']:
            continue
        cpu_difference = min(origin['cpu'] - target['cpu'], origin['cpu_recent'] - target['cpu_recent'])
        memory_difference = min(origin['memory'] - target['memory'], origin['memory_recent'] - target['memory_recent'])
        if min(origin['cpu'], origin['cpu_recent']) >= .5 and cpu_difference >= cpu_gap:
            reasons.append(f'CPU imbalance toward {name}: {cpu_difference * 100:.1f} percentage points from current measurements.')
        if min(origin['memory'], origin['memory_recent']) >= .5 and memory_difference >= ram_gap:
            reasons.append(f'Memory imbalance toward {name}: {memory_difference * 100:.1f} percentage points from current measurements.')
        if origin['assigned_memory'] is not None and target['assigned_memory'] is not None:
            commitment = origin['assigned_memory'] / origin['ram']
            commitment_gap = {'conservative': .5, 'balanced': .3, 'thorough': .15}[effort]
            if commitment >= .5 and commitment - target['assigned_memory'] / target['ram'] >= commitment_gap:
                reasons.append(f'Memory commitment imbalance toward {name}: source allocation is {commitment:.2f} times physical RAM.')
    return reasons


PREFERENCE_NOTE = 'Weighted placement preferences can improve.'


def preference_improvable(policy, node, targets):
    # True when a permitted move of one placement unit off this host lowers the weighted Should cost.
    for rule in policy.preferences:
        if not policy.count(rule, policy.placements):
            continue
        for resource in rule['guests']:
            if policy.placements.get(resource) != node:
                continue
            unit = policy.by_guest.get(resource, [resource])
            for target in sorted(policy.domain(unit) & targets - {node}):
                changes = dict.fromkeys(unit, target)
                if policy.preference_gain(changes) > 0 and policy.check(changes)[0]:
                    return True
    return False


def commitment_cost(host):
    return max(0, host['assigned_memory'] / host['ram'] - 1.2) ** 2 if host['assigned_memory'] is not None else 0


def target_pressure(sample, demand, now):
    limits = {'pressurecpusome': 20, 'pressureiosome': 10, 'pressurememorysome': 2, 'iowait': .1}
    if sample['source_at'] is None or not 0 <= now - sample['source_at'] <= 180:
        return 'Target pressure source is missing or stale.'
    for key, limit in limits.items():
        if sample[key] is None:
            return f'Target {key} is missing. Automatic DRS requires Proxmox VE 9 pressure metrics and permission to read them.'
        if sample[key] > limit:
            return f'Target {key} exceeds the admission limit.'
    return None


def allocation_variance(first, second):
    if first['assigned_memory'] is None or second['assigned_memory'] is None:
        return 0
    weight = first['ram'] / (first['ram'] + second['ram'])
    gap = first['assigned_memory'] / first['ram'] - second['assigned_memory'] / second['ram']
    return weight * (1 - weight) * gap ** 2


def candidate(guest, demand, source, target, models, policy, urgency, track='dynamic', effort='conservative', workload=None, recovery_only=False, minimum_score=None, scheduled=False, unit_member=False, preference=0):
    origin, destination = models[source], models[target]
    if any(h.get('cpu_settling') for h in (origin, destination)):
        return None, 'Waiting for one minute of fresh host CPU measurements after migration.'
    selection_reason, workload_score, disruption_priority = pressure_policy.selection(guest, origin, urgency != 'routine')
    ram = max(guest['mem_total'], guest['mem_host'])
    scale = cpu_scale(origin, destination)
    cpu = demand['peak_cores'] * scale
    reserve = max(destination['ram'] * .1, 2 * GIB)
    if destination['memory'] * destination['ram'] + ram + reserve > destination['ram']:
        return None, 'Target cannot fit configured/observed RAM plus its host reserve.'
    cpu_reserved_after = destination['cpu'] + cpu / destination['cores']
    if cpu_reserved_after > .8:
        return None, 'Target cannot reserve CPU for the guest one-hour p95 demand.'
    cpu_expected = demand['busy_cores'] * scale
    cpu_after = destination['cpu'] + cpu_expected / destination['cores']
    expected_ram = guest['mem_host']
    recovery = balloon.project(origin, destination, guest, expected_ram, guest['observed_at'])
    admission = None
    if origin.get('balloon_recovery_enabled') and urgency == 'routine' and (destination.get('balloon_target_percent') is not None or origin.get('balloon_recoverable') or destination.get('balloon_recoverable')):
        room = balloon.available(destination, margin=True)
        if room is None:
            return None, 'Target ballooning threshold is unavailable; recovery capacity cannot be verified.'
        if ram + destination.get('balloon_recoverable', 0) > room:
            return None, 'Target cannot reserve guest RAM and existing balloon recovery below its ballooning threshold with a 2-point margin.'
        admission = {'target_recoverable': destination.get('balloon_recoverable', 0), 'margin_percent': balloon.MARGIN_PERCENT}
    cpu_relief = demand['busy_cores'] / origin['cores']
    after_source = {**recovery['source'], 'cpu': max(0, origin['cpu'] - cpu_relief),
                    'cpu_live': max(0, origin['cpu_live'] - cpu_relief),
                    'running_guests': max(0, origin.get('running_guests', 1) - 1),
                    'cpu_recent': max(0, origin['cpu_recent'] - cpu_relief),
                    'assigned_memory': max(0, origin['assigned_memory'] - guest['mem_total']) if origin['assigned_memory'] is not None else None}
    after_target = {**recovery['target'], 'cpu': cpu_after, 'cpu_recent': cpu_after,
                    'cpu_live': destination['cpu_live'] + cpu_expected / destination['cores'],
                    'running_guests': destination.get('running_guests', 0) + 1,
                    'peak_cpu': cpu_reserved_after,
                    'assigned_memory': destination['assigned_memory'] + guest['mem_total'] if destination['assigned_memory'] is not None else None}
    allocation = distribution.improvement(origin, destination, workload)
    independent_distribution = allocation and allocation['gain'] > .01 and (guest['cpu'] or 0) <= .1 and sum(guest.get(k) or 0 for k in telemetry.RATES) < 10 * 1024**2
    # Until overload is sustained, a routine distribution move must justify itself without relief credit.
    deferred_relief = bool(selection_reason) and track == 'distribution' and urgency == 'routine'
    if selection_reason and not deferred_relief and not unit_member and not scheduled and not recovery_only and not independent_distribution:
        return None, selection_reason
    if allocation:
        after_source['distribution'], after_target['distribution'] = allocation['source'], allocation['target']
        if not unit_member and urgency == 'routine' and not dynamic_reasons(origin) and allocation['gain'] <= .01 and recovery['score'] <= .01 and preference <= 0:
            return None, 'This move would not improve the long-term distribution objective.'
    baseline_source, baseline_target = recovery['baseline_source'], recovery['baseline_target']
    before_risk = risk(origin['cpu'], baseline_source['memory']) + risk(destination['cpu'], baseline_target['memory'])
    after_risk = risk(after_source['cpu'], after_source['memory']) + risk(after_target['cpu'], after_target['memory'])
    benefit = 100 * (before_risk - after_risk) / max(before_risk, 1)
    if deferred_relief and not scheduled:
        benefit = min(0, benefit)
    spread_before = abs(origin['cpu'] - destination['cpu']) + abs(baseline_source['memory'] - baseline_target['memory'])
    spread_after = abs(after_source['cpu'] - after_target['cpu']) + abs(after_source['memory'] - after_target['memory'])
    balance = max(-10, min(10, 10 * (spread_before - spread_after)))
    commitments_known = origin['assigned_memory'] is not None and destination['assigned_memory'] is not None
    commitment = max(-20, min(20, 12 * (commitment_cost(origin) + commitment_cost(destination) - commitment_cost(after_source) - commitment_cost(after_target)))) if commitments_known else 0
    allocation_gain = max(-50, min(50, 100 * (allocation_variance(origin, destination) - allocation_variance(after_source, after_target))))
    balloon_gain = recovery['score']
    disk_rate = (guest['disk_read_bps'] or 0) + (guest['disk_write_bps'] or 0)
    io_review = (origin['io_pressure'] or 0) >= 5 and (origin['io_wait'] or 0) >= .05
    io_gain = min(12, (origin['io_pressure'] or 0) * min(.5, disk_rate / origin['guest_disk_bps'])) if io_review and origin['guest_disk_bps'] and (guest['pressureiosome'] or 0) >= 5 else 0
    # Guest CPU PSI shows time spent waiting for host CPU, comparable to CPU ready time.
    psi = demand.get('cpu_psi') or 0
    relieves = not destination.get('contended_guests') and after_target['cpu'] <= origin['cpu'] - .05
    contention = min(10, psi / 4) if cpu_demand.contended(demand) and relieves else 0
    io = sum(guest[key] or 0 for key in telemetry.RATES)
    base_cost = 2 + min(8, ram / (8 * GIB)) + min(5, io / (50 * 1024 ** 2))
    multiplier = policy['migration_cost_multiplier'] or 2
    effort_multiplier = {'conservative': 1, 'balanced': .5, 'thorough': .2}[effort] if track == 'distribution' else 1
    cost = base_cost * multiplier * effort_multiplier
    distribution_gain = allocation['gain'] if allocation else 0
    allocation_gain = 0 if allocation else allocation_gain
    recovery_priority = recovery_only
    if recovery_only:
        benefit, balance, commitment, allocation_gain, io_gain, distribution_gain, workload_score, contention = (
            min(0, value) for value in (benefit, balance, commitment, allocation_gain, io_gain, distribution_gain, workload_score, contention))
    raw_score = benefit + balance + commitment + allocation_gain + balloon_gain + io_gain + distribution_gain + workload_score + contention + preference - cost
    minimum_gain = .5 if track == 'distribution' and effort == 'thorough' else 2
    if minimum_score is not None:
        minimum_gain = min(minimum_gain, minimum_score)
    if not unit_member and urgency != 'maintenance' and (benefit + commitment + allocation_gain + balloon_gain + io_gain + distribution_gain + contention + preference < minimum_gain or raw_score <= (.5 if minimum_score is None else minimum_score)):
        return None, 'Benefit does not justify migration cost and stickiness.'
    score = max(.1, min(100, raw_score))
    return {'resource': guest['resource'], 'name': guest['metadata'].get('name') or guest['resource'],
            'guest_type': 'lxc' if guest['resource'].startswith('lxc/') else 'qemu',
            'migration_mode': 'restart' if guest['resource'].startswith('lxc/') else 'live',
            'requires_downtime': guest['resource'].startswith('lxc/'),
            'source': source, 'target': target, 'group_id': policy['group_id'], 'stickiness': policy['stickiness'],
            'policy_allowed': policy['allowed'], 'policy_reason': policy['reason'] if not policy['allowed'] else None,
            'urgency': urgency, 'score': round(score, 2), 'score_unit': 'priority points',
            'disruption_priority': disruption_priority, 'relief_policy': origin.get('relief'),
            'workload': workload, 'distribution_gain': round(distribution_gain, 2), 'selection_goal': 'balloon_recovery' if recovery_priority else 'balance',
            'score_breakdown': {'placement_preference': round(preference, 2), 'workload_selection': round(workload_score, 2), 'resource_relief': round(benefit, 2), 'balance_improvement': round(balance, 2),
                                'commitment_relief': round(commitment, 2), 'balloon_recovery': round(balloon_gain, 2),
                                'allocation_balance': round(allocation_gain, 2), 'effort_multiplier': effort_multiplier,
                                'distribution_gain': round(distribution_gain, 2),
                                'distribution_parts': {k: round(v, 2) for k, v in allocation['parts'].items()} if allocation else {},
                                'io_opportunity': round(io_gain, 2), 'contention_relief': round(contention, 2), 'guest_cpu_psi': round(psi, 2),
                                'migration_cost': round(cost, 2), 'stickiness_multiplier': multiplier},
            'before': round(max(origin['cpu'], origin['memory'], destination['cpu'], destination['memory']), 4),
            'after': round(max(after_source['cpu'], after_source['memory'], after_target['cpu'], after_target['memory']), 4),
            'projection': {'source_before': origin.copy(), 'target_before': destination.copy(),
                           'source_after': after_source, 'target_after': after_target},
            'reserved_memory': ram, 'reserved_cpu_cores': cpu, 'coverage': demand['coverage'],
            'measurement_basis': 'five-minute mean CPU, one-hour p95 CPU reservation, current RAM', 'metrics_observed_at': guest['observed_at'],
            'expected_memory': expected_ram, 'expected_cpu_cores': cpu_expected,
            'potential_balloon_recovery': max(0, recovery['gain_bytes']), 'source_balloon_refill': recovery['source_refill'],
            'target_balloon_refill': recovery['target_refill'], 'balloon_admission': admission,
            'requires_storage_review': io_review,
            'io_assumption': 'I/O relief is hypothetical until storage topology confirms that migration leaves the bottleneck. Shared storage may remain saturated.' if io_review else None,
            'requires_preflight': True, 'execution_allowed': False,
            'cost_quality': 'Proxy from RAM and current traffic; transfer duration and dirty-page rate are unknown.',
            'reason': 'Create additional capacity for balloon recovery. Recovery benefit must outweigh migration cost and any worsening of other placement objectives.' if recovery_priority else 'Reduce sustained imbalance, allocation skew or ballooning. Expected load includes resident RAM and native balloon recovery; admission reserves configured RAM, one-hour p95 guest CPU demand and host headroom.',
            'override': policy.get('code') == 'stickiness_override' or policy.get('override', False)}, None


def evaluate(db, connection_id, now, previous=None, maintenance_source=None, manual=True, run_now=False, excluded_resources=(), continuous=False, recovery_only=False, scheduled=False, rule_only=False, aggressive=False, guest_kind='qemu'):
    scheduled = scheduled or aggressive
    configuration = settings(db, connection_id)
    guest_kind = 'all' if maintenance_source else guest_kind
    if guest_kind not in ('qemu', 'lxc', 'all') or guest_kind == 'all' and not maintenance_source:
        raise ValueError('Choose VM or LXC balancing; mixed scope is reserved for maintenance.')
    if continuous and guest_kind != 'qemu':
        raise ValueError('Continuous balancing only migrates VMs.')
    configuration = {**configuration, 'guest_kind': guest_kind, 'container_balancing_enabled': guest_kind in ('lxc', 'all')}
    adaptive_policy = adaptive.profile(configuration) if continuous else None
    effort = 'thorough' if aggressive else adaptive_policy['effort'] if continuous else configuration['distribution_effort']
    movement_history = adaptive.movements(db, connection_id, now)
    pair_flows = adaptive.flows(movement_history)
    # Scheduled runs ignore failure penalties only; an explicit aggressive run also skips history protection.
    failure_costs = {} if scheduled else migration_failures.costs(db, connection_id, now)
    preflight_rejections = {} if scheduled else preflight_failures.pending(db, connection_id, now)
    schedule = planning_schedule.describe(db, connection_id, configuration, now, manual=manual, run_now=run_now, guest_kind=guest_kind)
    if continuous:
        schedule.update(active=True, continuous=True, new_run=False)
    if scheduled:
        schedule.update(active=True, new_run=False)
    samples = {s['resource']: s for s in telemetry.latest(db, connection_id, now)}
    profiles = demand_profiles(db, connection_id, samples, now)
    host_performance = performance(db, connection_id)
    ha_movable = ha_guests.movable(db, connection_id) if configuration['ha_guests_enabled'] else set()
    groups = migration_groups.list_groups(db, connection_id)
    maintenance_nodes = node_maintenance.excluded(db, connection_id) | ha_guests.maintenance_hosts(ha_guests.cached(db, connection_id))
    policies = {row['resource']: row['stickiness'] for row in db.execute('SELECT resource,stickiness FROM guest_policies WHERE connection_id=?', (connection_id,))}
    infrastructure_status = infrastructure.latest(db, connection_id, now)
    storage_checks = storage.checks(db, connection_id, now)
    storage_policy = {resource: {key: value for key, value in check.items() if key != 'checked_at'} for resource, check in storage_checks.items()}
    balloon_policy = {s['node']: s['metadata'].get('balloon_config', {}).get('target_percent') for s in samples.values() if s['resource'].startswith('node/')}
    rule_policy = placement_rules.Policy(db, connection_id, [{**s['metadata'].get('inventory', {}), 'id': r, 'type': r.split('/')[0], 'node': s['node'], 'status': s['status']} for r, s in samples.items()])
    policy_key = hashlib.sha256(json.dumps([groups, policies, configuration, storage_policy, sorted(maintenance_nodes), balloon_policy, recovery_only, rule_policy.key, host_performance], sort_keys=True).encode()).hexdigest()[:16]
    observation_gap = max(60, 2 * configuration['continuous_interval_seconds']) if continuous else INTERVAL + 60
    consistent = bool(previous and previous.get('algorithm_version') == VERSION and previous.get('policy_key') == policy_key and 0 <= now - previous['evaluated_at'] <= observation_gap)
    previous = previous if consistent else {}
    nodes = {s['node']: s['metadata'].get('inventory', {}) for s in samples.values() if s['resource'].startswith('node/')}
    result = {'infrastructure': infrastructure_status, 'evaluated_at': int(now), 'valid_until': int(now) + INTERVAL + 60, 'algorithm_version': VERSION,
              'policy_key': policy_key, 'interval_seconds': configuration['continuous_interval_seconds'] if continuous else INTERVAL, 'retention_days': 7,
              'automatic_execution_enabled': (configuration['continuous_enabled'] or configuration['distribution_enabled'] or configuration['container_schedule']['enabled']) and not configuration['paused'], 'mode': 'aggressive_manual' if aggressive else 'scheduled' if scheduled else 'continuous' if continuous else 'observe', 'settings': configuration,
              'schedule': schedule, 'evaluation_kind': 'aggressive_run_now' if aggressive else 'scheduled' if scheduled else 'continuous' if continuous else 'run_now' if run_now else 'manual' if manual else 'scheduled' if schedule['new_run'] else 'dynamic',
              'continuous_policy': adaptive_policy, 'placement_rules': rule_policy.statuses(),
              'proposals': [], 'candidates': [], 'blocked': [], 'hosts': [], 'pressure_streaks': {}, 'pressure_since': {},
              'status': 'observing', 'message': 'No sustained imbalance or overload requires a move.', 'plan': [], 'diagnostic_plan': [],
              'strategy': 'gradual', 'maintenance_source': maintenance_source, 'guest_kind': guest_kind,
              'limits': {'routine_moves_before_recheck': 1, 'critical_parallel_limit': 3, 'critical_per_host_limit': 2,
                         'settle_seconds': 300, 'guest_cooldown_seconds': 3600},
              'limitations': 'Scores are estimates, not VMware VM DRS scores. Live compatibility, storage and capacity checks remain required. ' + (
                  'Aggressive Run now ignores history penalties, cooldowns, return and swap protection; each guest is attempted at most once per run.' if aggressive else
                  'Scheduled execution keeps guest cooldowns, six-hour return protection and swap protection but ignores migration failure penalties; each guest is attempted at most once per run.')}
    if scheduled:
        result['limits'].update(guest_cooldown_seconds=0 if aggressive else adaptive.SCHEDULED_POLICY['cooldown_seconds'], settle_seconds=60,
                                return_window_seconds=0 if aggressive else adaptive.RETURN_WINDOW, minimum_score=configuration['scheduled_minimum_score'])
    if continuous:
        result['limits'].update(critical_parallel_limit=1, critical_per_host_limit=1,
                                settle_seconds=adaptive_policy['settle_seconds'], guest_cooldown_seconds=adaptive_policy['cooldown_seconds'])
    models, hot, blocked, needs, tracks = {}, {}, Counter(), {}, {}
    changed_hosts = placement_load.changes(db, connection_id, now, WINDOW)
    for name, inventory in nodes.items():
        sample, demand = samples[f'node/{name}'], profiles.get(f'node/{name}')
        valid = (fresh(sample, now) and inventory.get('status') == 'online' and demand['coverage'] == 1
                 and sample['mem_total'] and sample['cpu_count'] and sample['mem_used'] is not None and sample['cpu'] is not None)
        if not valid:
            reason = 'Host is offline.' if inventory.get('status') != 'online' else 'Host needs complete CPU/RAM measurements no older than 45 seconds.'
            blocked[reason] += 1
            result['hosts'].append({'node': name, 'state': 'offline' if inventory.get('status') != 'online' else 'insufficient_data',
                                    'coverage': demand['coverage'], 'placement_reasons': [reason], 'metrics_observed_at': sample['observed_at']})
            continue
        guests = [s for s in samples.values() if s['node'] == name and s['resource'].startswith('qemu/') and s['status'] == 'running']
        models[name] = {**model(sample, demand), **memory_commitments(samples, name, now),
                        **balloon.host_fields(sample, guests, now, configuration['balloon_recovery_enabled'] or recovery_only)}
        placement_load.correct(db, connection_id, models[name], sample, changed_hosts.get(name), now)
        prior_host = next((h for h in previous.get('hosts', []) if h['node'] == name), None)
        models[name]['relief'] = pressure_policy.evidence(db, connection_id, sample, samples, now, configuration, prior_host)
        models[name]['contended_guests'] = sorted(r for r, s in samples.items() if s['node'] == name and r.startswith(('qemu/', 'lxc/'))
                                                  and s['status'] == 'running' and cpu_demand.contended(profiles.get(r)))
        models[name]['migration_group_id'] = next((g['id'] for g in groups if name in g['nodes']), None)
        models[name]['performance'] = host_performance.get(name, 1)
        if sample['source_at'] is None or not 0 <= now - sample['source_at'] <= 180:
            for key in ('cpu_pressure', 'memory_pressure', 'io_pressure'):
                models[name][key] = None
    domains, workloads = distribution.prepare(models, samples, groups, maintenance_nodes, now, profiles) if not maintenance_source else ([], {})
    for domain in domains:
        if not domain['available']:
            blocked[f'Workload distribution is unverified for {domain["name"]}: {domain["reason"]}'] += 1
    preference_targets = {n for n in models if n not in maintenance_nodes and n != maintenance_source
                          and not infrastructure_status.get(n, {}).get('block_reason')}
    for name in models:
        sample, demand = samples[f'node/{name}'], profiles[f'node/{name}']
        if name in maintenance_nodes and name != maintenance_source:
            result['hosts'].append({'node': name, **models[name], 'state': 'maintenance', 'coverage': demand['coverage'], 'placement_reasons': ['Host is excluded from ProxHA placement until Exit maintenance.']})
            continue
        critical = confirmed_pressure(db, connection_id, sample['resource'], now)
        needs[name] = placement_reasons(name, models, name in previous.get('pressure_since', {}), effort=effort)
        tracks[name] = 'maintenance' if name == maintenance_source else 'dynamic' if critical or dynamic_reasons(models[name]) else 'distribution'
        if needs[name] or critical or name == maintenance_source:
            hot[name] = 'maintenance' if name == maintenance_source else 'critical' if critical else 'routine'
            previous_track = next((host.get('evaluation_track') for host in previous.get('hosts', []) if host['node'] == name), None)
            result['pressure_since'][name] = previous.get('pressure_since', {}).get(name, int(now)) if previous_track == tracks[name] else int(now)
            result['pressure_streaks'][name] = min(3, previous.get('pressure_streaks', {}).get(name, 0) + 1)
        state = 'imbalanced' if hot.get(name) == 'routine' else hot.get(name, 'healthy')
        # Should preferences only adjust scores; the note is informational and never changes host state.
        if preference_improvable(rule_policy, name, preference_targets):
            needs[name] = needs[name] + [PREFERENCE_NOTE]
        result['hosts'].append({'node': name, **models[name], 'state': state, 'coverage': demand['coverage'], 'evidence': critical,
                                'placement_reasons': needs[name]})
        result['hosts'][-1]['evaluation_track'] = tracks[name] if name in hot else None
    result['imbalance'] = {key: max((m[key] for m in models.values()), default=0) - min((m[key] for m in models.values()), default=0)
                           for key in ('cpu', 'memory')}
    if any(value == 'critical' for value in hot.values()):
        result['strategy'] = 'critical'
    if maintenance_source:
        result['strategy'] = 'maintenance'
        hot = {name: state for name, state in hot.items() if name == maintenance_source}
    active = db.execute("SELECT 1 FROM operation_tasks t JOIN operations o ON o.id=t.operation_id WHERE o.connection_id=? AND t.status IN ('submitting','running','unknown') LIMIT 1", (connection_id,)).fetchone()
    active = active or db.execute("SELECT 1 FROM native_tasks WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND status IN ('running','unknown') LIMIT 1", (connection_id,)).fetchone()
    backup = backup_guard.read(db, connection_id, now)
    result['backup_guard'] = backup
    if backup['blocked']:
        blocked[backup['message']] += 1
        active = True
    recent_moves = {row['resource'] for row in db.execute("SELECT resource FROM native_tasks WHERE connection_id=? AND kind IN ('qmigrate','vzmigrate') AND status='completed' AND COALESCE(finished_at,observed_at) BETWEEN ? AND ? UNION SELECT resource FROM placement_events WHERE connection_id=? AND observed_at BETWEEN ? AND ?",
                                                        (connection_id, now - 3600, now, connection_id, now - 3600, now))}
    if active:
        blocked['An active or unresolved operation prevents a new plan.'] += 1
    if not any(group['enabled'] and len(group['nodes']) >= 2 for group in groups):
        blocked['Create an enabled migration domain with at least two hosts.'] += 1
    from .placement_planner import Planner
    rule_planner = Planner(db, connection_id, now, rule_policy, samples, profiles, models, configuration,
        storage_checks, policies, hot, effort, continuous, scheduled, movement_history,
        failure_costs, adaptive_policy, excluded_resources, workloads, recent_moves, aggressive)
    eligible, native_guests = [], set()
    for resource, guest in samples.items():
        if not resource.startswith(('qemu/', 'lxc/')) or (maintenance_source and guest['node'] != maintenance_source):
            continue
        if resource.partition('/')[0] not in migration_groups.balancing_types(guest_kind):
            continue
        if rule_only or len(rule_policy.by_guest.get(resource, [])) > 1:
            continue
        storage_check = storage_checks.get(resource)
        storage_reason = storage.block_reason(storage_check, guest['node'])
        if guest['node'] not in hot and not storage_reason:
            continue
        source, inventory = guest['node'], guest['metadata'].get('inventory', {})
        demand = profiles.get(resource)
        ha = inventory.get('ha') or {}
        if maintenance_source and ha.get('managed') and guest['status'] == 'running' and ha.get('state', 'started') not in native_ha.UNMANAGED:
            # Native HA maintenance evacuates its own resources before ProxHA moves the remaining guests.
            native_guests.add(resource)
            result['candidates'].append({'resource': resource, 'name': guest['metadata'].get('name') or resource, 'source': source,
                                         'target': None, 'score': None, 'status': 'native_ha', 'urgency': hot.get(source, 'routine'),
                                         'reasons': ['Native HA evacuates this HA resource when the host enters maintenance and chooses its target.'],
                                         'storage': storage_check, 'migration_failures': failure_costs.get(resource)})
            continue
        reason = None
        if resource in excluded_resources:
            reason = 'Already attempted in this run; it will not be submitted again.'
        elif guest['status'] != 'running':
            reason = 'Not running; no balancing move is needed.'
        elif storage_reason:
            reason = storage_reason
        elif resource.startswith('lxc/') and not configuration['container_balancing_enabled']:
            reason = 'Container balancing is disabled in DRS Settings. Running containers require restart migration with downtime.'
        elif ha.get('managed') and maintenance_source:
            reason = (f'Native HA does not move {ha.get("state")} HA resources during maintenance. '
                      'Migrate it or change its HA state in Proxmox first.')
        elif ha.get('managed') and resource not in ha_movable:
            reason = ('HA-managed guest: native HA owns placement. Enable HA guest balancing in DRS Settings; '
                      'guests in native HA groups or affinity rules always stay with native HA.')
        elif (continuous or scheduled) and policies.get(resource, 5) == 10:
            reason = 'Stickiness 10 blocks automatic balancing, including pressure relief. Use maintenance for evacuation.'
        elif guest['mem_host'] is None:
            reason = 'Guest host memory (memhost) is unavailable. Automatic DRS requires Proxmox VE 9 guest metrics and permission to read them.'
        elif not fresh(guest, now) or not demand or demand['coverage'] < 1 or demand['peak_cores'] is None or demand['busy_cores'] is None or not guest['mem_total'] or guest['mem_host'] is None or guest['cpu'] is None or not guest['cpu_count']:
            reason = 'Guest needs complete CPU/RAM measurements no older than 45 seconds.'
        elif not aggressive and (demand['nodes'] != {source} or resource in recent_moves) and hot[source] == 'routine':
            reason = 'Placement changed in the last hour; guest cooldown is active.'
        if reason:
            blocked[reason] += 1
            result['candidates'].append({'resource': resource, 'name': guest['metadata'].get('name') or resource, 'source': source,
                                         'target': None, 'score': None, 'status': 'blocked', 'reasons': [reason],
                                         'urgency': hot.get(source, 'routine'), 'storage': storage_check,
                                         'migration_failures': failure_costs.get(resource)})
            continue
        eligible.append(guest)

    def choices(guest, current_models, diagnostic=False, placements=None):
        source, resource = guest['node'], guest['resource']
        urgency = hot[source]
        failures, options = [], []
        for target in sorted(nodes):
            if target == source:
                continue
            rejection = preflight_failures.reason(preflight_rejections, resource, source, target, now)
            if rejection:
                failures.append(f'{target}: {rejection}')
                continue
            if target not in models or target not in current_models or target == maintenance_source or target in maintenance_nodes or hot.get(target) == 'critical':
                host = next((h for h in result['hosts'] if h['node'] == target), {})
                reason = ('Host is excluded until Exit maintenance.' if target in maintenance_nodes else
                          'Host is under critical pressure.' if hot.get(target) == 'critical' else
                          '; '.join(host.get('placement_reasons', [])) or 'Fresh host CPU/RAM measurements are unavailable.')
                failures.append(f'{target}: {reason}')
                continue
            if infrastructure_status.get(target, {}).get('block_reason'):
                failures.append(f'{target}: {infrastructure_status[target]["block_reason"]}')
                continue
            storage_reason = storage.block_reason(storage_checks.get(resource), source, target)
            if storage_reason:
                failures.append(f'{target}: {storage_reason}')
                continue
            intent = 'maintenance' if urgency == 'maintenance' else 'emergency' if urgency == 'critical' else 'balance'
            policy = migration_groups.automatic_migration_policy(groups, nodes, guest['metadata']['inventory'], target,
                       restart_containers=configuration['container_balancing_enabled'],
                       stickiness=policies.get(resource, 5), intent=intent,
                       exception_reason=f'{urgency} preview; no execution authorized.' if intent != 'balance' else None)
            missing_group = policy.get('code') == 'unassigned'
            if missing_group and any(not group['enabled'] and (source in group['nodes'] or target in group['nodes']) for group in groups):
                failures.append(f'{target}: A participating migration domain is disabled.')
                continue
            if missing_group:
                movement = guest_policy.preference(policies.get(resource, 5), intent,
                            f'{urgency} preview; no execution authorized.' if intent != 'balance' else None)
                if not movement['allowed']:
                    failures.append(f'{target}: {movement["reason"]}')
                    continue
                policy = {**movement, **policy, 'override': movement.get('code') == 'stickiness_override'}
            reason = policy['reason'] if not policy['allowed'] and not missing_group else target_pressure(samples[f'node/{target}'], profiles[f'node/{target}'], now)
            if reason:
                failures.append(f'{target}: {reason}')
                continue
            allowed, rule_reason, overrides = rule_policy.check({resource: target}, placements, maintenance=bool(maintenance_source))
            if not allowed:
                failures.append(f'{target}: {rule_reason}')
                continue
            scoring_track = 'distribution' if scheduled or (run_now or continuous) and urgency == 'routine' else tracks[source]
            demand = profiles[resource]
            churn = None
            if not aggressive and (continuous or scheduled or urgency == 'routine'):
                # Swap protection applies to routine moves; critical relief and maintenance are exempt.
                churn = adaptive.movement_cost(movement_history.get(resource, []), target, now, adaptive.churn_policy(continuous, scheduled, adaptive_policy),
                                               source if urgency == 'routine' else None, pair_flows)
                if churn['reason']:
                    failures.append(f'{target}: {churn["reason"]}')
                    continue
            move, reason = candidate(guest, demand, source, target, current_models, policy, urgency, scoring_track, effort, workloads.get(resource), recovery_only=recovery_only,
                                     minimum_score=configuration['scheduled_minimum_score'] if scheduled else None, scheduled=scheduled, preference=rule_policy.preference_gain({resource: target}, placements))
            if move and recovery_only and move['potential_balloon_recovery'] <= balloon.MIN_GAIN:
                move, reason = None, 'This move does not release additional balloon recovery capacity.'
            failure = failure_costs.get(resource, {'penalty': 0, 'failures_7d': 0})
            if move:
                move['score'] = round(move['score'] - failure['penalty'], 2)
                move['score_breakdown']['migration_failure_penalty'] = failure['penalty']
                move['migration_failures'] = failure
                if failure['penalty'] and move['score'] < (adaptive_policy['minimum_score'] if continuous else .5):
                    reason = f'Migration failure penalty {failure["penalty"]} points leaves score {move["score"]:.2f} below the required benefit. Each failure decays by one point per minute.'
                    move = None
            if move and continuous:
                score = move['score'] - churn['penalty']
                move.update(score=round(score, 2), movement_history=churn)
                move['score_breakdown'].update(migration_history_penalty=churn['penalty'])
                if score < adaptive_policy['minimum_score']:
                    move, reason = None, f'History-adjusted score {score:.2f} is below the continuous threshold {adaptive_policy["minimum_score"]}.'
            elif move and churn:
                move['movement_history'] = churn
            if move:
                move['storage'] = storage_checks[resource]
                move['placement_overrides'] = overrides
            if move and (diagnostic or move['policy_allowed']):
                options.append(move)
            elif move:
                failures.append(f'{target}: {policy["reason"]}')
            else:
                failures.append(f'{target}: {reason}')
        return sorted(options, key=lambda move: (not move['policy_allowed'], -move['score'], move['target'])), failures

    previous_candidates = {c['resource']: c for c in previous.get('candidates', [])}
    for guest in eligible:
        options, failures = choices(guest, models, diagnostic=True)
        if not options:
            result['candidates'].append({'resource': guest['resource'], 'name': guest['metadata'].get('name') or guest['resource'],
                                         'source': guest['node'], 'target': None, 'score': None, 'status': 'blocked',
                                         'urgency': hot[guest['node']], 'reasons': failures or ['No compatible target exists.'],
                                         'storage': storage_checks.get(guest['resource']),
                                         'migration_failures': failure_costs.get(guest['resource'])})
            blocked['No target passes policy, capacity and benefit checks.'] += 1
            continue
        move = options[0]
        move['placement_reasons'] = needs[guest['node']]
        move['evaluation_track'] = tracks[guest['node']]
        prior = previous_candidates.get(guest['resource'], {})
        same = prior.get('target') == move['target'] and prior.get('source') == move['source'] and prior.get('status') not in ('blocked', 'paused', 'policy_blocked', 'schedule_disabled') and prior.get('urgency') == move['urgency'] and prior.get('evaluation_track') == move['evaluation_track']
        first_seen = prior['first_seen_at'] if same and 'first_seen_at' in prior else int(now)
        hold = 0 if scheduled or move.get('rule_repair') else adaptive_policy['hold_seconds'] if continuous else configuration['hold_seconds'] if move['urgency'] == 'routine' else 0
        delay = 0 if scheduled or move.get('rule_repair') else adaptive_policy['hold_seconds'] if continuous else configuration['delay_seconds'] if move['urgency'] == 'routine' else 30
        ready_at = max(first_seen + delay, result['pressure_since'].get(move['source'], now) + hold)
        move.update(first_seen_at=first_seen, ready_at=int(ready_at), remaining_seconds=max(0, math.ceil(ready_at - now)),
                    status='waiting' if ready_at > now else 'review_ready', reasons=failures,
                    alternatives=[{'target': item['target'], 'score': item['score']} for item in options[1:]])
        if move['evaluation_track'] == 'distribution' and not schedule['active']:
            move.update(status='scheduled' if schedule['enabled'] else 'schedule_disabled',
                        ready_at=max(int(ready_at), schedule['next_at']) if schedule['enabled'] else None)
        if active or configuration['paused']:
            move['status'] = 'paused'
        if not move['policy_allowed']:
            move.update(status='policy_blocked', ready_at=None, remaining_seconds=None)
            blocked[move['policy_reason']] += 1
        move['remaining_seconds'] = max(0, math.ceil(move['ready_at'] - now)) if move['ready_at'] is not None and move['status'] != 'paused' else None
        result['candidates'].append(move)
    rule_options, rule_blocked = rule_planner.options()
    if recovery_only:
        rule_options = [m for m in rule_options if not m['rule_repair'] and m['potential_balloon_recovery'] > balloon.MIN_GAIN]
    if rule_only:
        rule_options = [m for m in rule_options if m['rule_repair']]
    for move in rule_options:
        prior = previous_candidates.get(move['resource'], {})
        same = prior.get('target') == move['target'] and prior.get('unit_members') == move['unit_members']
        first_seen = prior.get('first_seen_at', int(now)) if same else int(now)
        hold = 0 if move['rule_repair'] or scheduled else adaptive_policy['hold_seconds'] if continuous else configuration['hold_seconds']
        ready_at = first_seen + hold
        move.update(first_seen_at=first_seen, ready_at=ready_at, remaining_seconds=max(0, ready_at - now),
                    status='review_ready' if ready_at <= now else 'waiting', placement_reasons=[move['reason']], reasons=[])
        if not move['rule_repair'] and not schedule['active']:
            move.update(status='scheduled' if schedule['enabled'] else 'schedule_disabled', ready_at=schedule['next_at'])
        if active or configuration['paused']:
            move['status'] = 'paused'
        result['candidates'] = [c for c in result['candidates'] if c['resource'] != move['resource']]
        result['candidates'].append(move)
    result['candidates'].extend(rule_blocked)
    result['candidates'].sort(key=lambda c: (c['score'] is None, not c.get('rule_repair'), not c.get('policy_allowed', True), {'critical': 0, 'maintenance': 0, 'routine': 1}[c['urgency']], len(c.get('unit_moves', [])) > 1, c.get('evaluation_track') == 'distribution', c.get('disruption_priority', 0), -(c['score'] or 0), c['resource']))
    ranked = [c for c in result['candidates'] if c['score'] is not None]
    ranked_ids = {c['resource'] for c in ranked}
    for rank, move in enumerate(ranked, 1):
        move['rank'] = rank
    def build_plan(diagnostic=False):
        simulated, moved, waves, plan = deepcopy(models), set(), [], []
        placements = rule_policy.placements.copy()
        limit = 32 if any(d['available'] for d in domains) else 12 if result['strategy'] != 'gradual' or effort == 'thorough' else 5
        if not active and not configuration['paused']:
            for step in range(limit):
                options = []
                for guest in eligible:
                    source = guest['node']
                    projected = any(move['source'] == source for move in plan)
                    if guest['resource'] not in ranked_ids or guest['resource'] in moved or (hot[source] != 'maintenance' and not placement_reasons(source, simulated, continuing=True, projected=projected, effort=effort)):
                        continue
                    choices_now, _ = choices(guest, simulated, diagnostic=diagnostic, placements=placements)
                    if choices_now:
                        options.append(choices_now[0])
                unit_options, _ = rule_planner.options(simulated, placements, moved)
                options.extend(m for m in unit_options if m['resource'] in ranked_ids and
                               (not rule_only or m['rule_repair']) and
                               (not recovery_only or not m['rule_repair'] and m['potential_balloon_recovery'] > balloon.MIN_GAIN))
                if not options:
                    break
                options.sort(key=lambda c: (not c.get('rule_repair'), {'critical': 0, 'maintenance': 0, 'routine': 1}[c['urgency']], len(c.get('unit_moves', [])) > 1, tracks.get(c['source']) == 'distribution', c.get('disruption_priority', 0), -c['score'], c['resource'], c['target']))
                move = options[0]
                if move['requires_storage_review'] and plan:
                    break
                moved.update(move.get('unit_members', [move['resource']]))
                for member in move.get('unit_moves', [move]):
                    placements[member['resource']] = member['target']
                if move.get('unit_projection'):
                    simulated = move['unit_projection']
                else:
                    simulated[move['source']] = move['projection']['source_after']
                    simulated[move['target']] = move['projection']['target_after']
                current = next(c for c in ranked if c['resource'] == move['resource'])
                move['evaluation_track'] = current['evaluation_track']
                wave = step + 1
                if result['strategy'] != 'gradual':
                    for index, members in enumerate(waves):
                        used = Counter(node for item in members for node in (item['source'], item['target']))
                        if len(members) < 3 and used[move['source']] < 2 and used[move['target']] < 2 and not any(item['source'] == move['target'] or item['target'] == move['source'] for item in members):
                            wave = index + 1
                            break
                    else:
                        wave = len(waves) + 1
                    if wave > len(waves):
                        waves.append([])
                    waves[wave - 1].append(move)
                same_target = move['target'] == current['target']
                hold = 0 if scheduled or move.get('rule_repair') else adaptive_policy['hold_seconds'] if continuous else configuration['hold_seconds'] if move['urgency'] == 'routine' else 0
                delay = 0 if scheduled or move.get('rule_repair') else adaptive_policy['hold_seconds'] if continuous else configuration['delay_seconds'] if move['urgency'] == 'routine' else 30
                ready_at = current['ready_at'] if same_target else max(int(now) + delay, result['pressure_since'].get(move['source'], now) + hold)
                move.update(order=step + 1, wave=wave, status=current['status'] if same_target else 'waiting', ready_at=ready_at,
                            conditional=step > 0, recheck_after=True)
                if current['status'] in ('scheduled', 'schedule_disabled'):
                    move.update(status=current['status'], ready_at=max(ready_at, schedule['next_at']) if current['status'] == 'scheduled' else None)
                if not move['policy_allowed']:
                    move.update(status='policy_blocked', ready_at=None)
                plan.append(move)
                if move['requires_storage_review']:
                    break
        return plan, simulated

    result['plan'], simulated = build_plan()
    if not result['plan'] and any(not c['policy_allowed'] for c in ranked):
        result['diagnostic_plan'], _ = build_plan(diagnostic=True)
    result['proposals'] = [move for move in result['plan'] if move['status'] == 'review_ready']
    result['blocked'] = [{'reason': reason, 'count': count} for reason, count in sorted(blocked.items())]
    result['plan_summary'] = {'moves': len(result['plan']), 'waves': max((m['wave'] for m in result['plan']), default=0),
                              'before': models, 'after': simulated, 'requires_revalidation': True}
    result['distribution'] = distribution.describe(domains, models, simulated, result['plan'])
    result['balloon_recovery'] = balloon.describe(result['hosts'])
    balloon.progress(db, connection_id, samples, now, result['balloon_recovery'])
    result['balloon_recovery']['enabled'] = configuration['balloon_recovery_enabled'] or recovery_only
    result['balloon_recovery']['projected'] = balloon.describe([{'node': name, **host} for name, host in simulated.items()])
    result['goal'] = 'balloon_recovery' if recovery_only else 'balance'
    if configuration['paused']:
        result.update(status='paused', message='Planning is paused. Collection continues; automatic execution is disabled.')
    elif backup['blocked']:
        result.update(status='backup_paused', message=backup['message'])
    elif result['diagnostic_plan']:
        result.update(status='policy_blocked', message='Useful capacity placements exist, but migration domain membership must be configured before planning permitted moves.')
    elif ranked:
        result.update(status='proposals' if result['proposals'] else 'watching',
                      message='Ranked candidates are a preview. Reassess after each move; later steps may be discarded.')
    elif blocked:
        result.update(status='blocked', message='Planning is blocked. Review the constraints and candidate exclusions below.')
    elif hot:
        result.update(status='no_feasible_move', message='Imbalance is present, but no candidate currently passes capacity, benefit and policy checks.')
    if maintenance_source:
        residents = [s for s in samples.values() if s['resource'].startswith(('qemu/', 'lxc/')) and s['node'] == maintenance_source and s['status'] == 'running']
        planned = {m['resource'] for m in result['plan']}
        result['maintenance'] = {'state': 'blocked' if maintenance_source not in models or any(s['resource'] not in planned | native_guests for s in residents) else 'planned',
                                 'running_guests': len(residents), 'planned_guests': len(planned), 'native_ha_guests': len(native_guests),
                                 'changes_host_state': False}
    for move in result['candidates'] + result['plan'] + result['diagnostic_plan']:
        move.pop('unit_projection', None)
        for member in move.get('unit_moves', []):
            member.pop('projection', None)
    return result


def save_evaluation(db, connection_id, now, manual=True, run_now=False, excluded_resources=(), recovery_only=False, scheduled=False, aggressive=False, guest_kind='qemu'):
    result = evaluate(db, connection_id, now, None if scheduled or aggressive else latest(db, connection_id), manual=manual, run_now=run_now, excluded_resources=excluded_resources, recovery_only=recovery_only, scheduled=scheduled, aggressive=aggressive, guest_kind=guest_kind)
    if not aggressive:
        planning_schedule.record(db, connection_id, result['schedule'], now)
    db.execute('INSERT OR REPLACE INTO balancing_evaluations VALUES (?,?,?)', (connection_id, int(now), json.dumps(result, separators=(',', ':'))))
    return result


def tick(database, now):
    with database() as db:
        prune(db, now)
        ids = [row['id'] for row in db.execute('SELECT id FROM connections')]
    for connection_id in ids:
        try:
            with database() as db:
                if settings(db, connection_id)['continuous_enabled']:
                    continue
                previous = latest(db, connection_id)
                if previous and previous.get('algorithm_version') == VERSION and int(previous['evaluated_at'] // INTERVAL) >= int(now // INTERVAL):
                    continue
                save_evaluation(db, connection_id, now, manual=False)
        except Exception:
            logging.getLogger(__name__).exception('Balancing evaluation failed for connection %s.', connection_id)

