from copy import deepcopy

from . import adaptive, ha_guests, migration_groups, placement_repair, placement_rules, storage


class Planner:
    def __init__(self, db, connection_id, now, policy, samples, profiles, models,
                 configuration, checks, policies, hot, effort, continuous, scheduled,
                 movement_history, failure_costs, adaptive_policy, excluded, workloads, recent_moves, aggressive=False):
        self.db, self.connection_id, self.now, self.policy = db, connection_id, now, policy
        self.samples, self.profiles, self.models = samples, profiles, models
        self.config, self.checks, self.policies, self.hot = configuration, checks, policies, hot
        self.effort, self.continuous, self.scheduled = effort, continuous, scheduled
        self.movements, self.failures, self.aggressive = movement_history, failure_costs, aggressive
        self.flows = adaptive.flows(movement_history)
        self.adaptive, self.excluded = adaptive_policy, set(excluded)
        self.workloads, self.recent_moves = workloads, recent_moves
        self.repair_models = placement_repair.host_models(samples, now)
        from . import balancing, infrastructure
        self.infrastructure = infrastructure.latest(db, connection_id, now)
        speed = balancing.performance(db, connection_id)
        for node, model in self.repair_models.items():
            model['performance'] = speed.get(node, 1)
        self.preflight_failures = placement_rules.failures(db, connection_id, now)

    def options(self, models=None, placements=None, attempted=()):
        models = self.models if models is None else models
        placements = self.policy.placements if placements is None else placements
        options, blocked = [], []
        for unit in self.policy.units:
            relevant = [r for r in self.policy.rules if set(unit).intersection(r['guests'])]
            broken = any(self.policy.count(r, placements) for r in relevant)
            if not relevant or len(unit) < 2 and not broken:
                continue
            candidates, reasons = [], []
            intent = self.db.execute('SELECT target FROM placement_intents WHERE connection_id=? AND unit=? AND policy_key=?',
                                     (self.connection_id, '|'.join(unit), self.policy.key)).fetchone()
            for target in sorted(self.policy.domain(unit)):
                changing = [r for r in unit if placements[r] != target]
                if not changing:
                    continue
                if set(changing).intersection(set(attempted) | self.excluded):
                    reasons.append('A member has already been attempted in this run.')
                    continue
                failure = self.preflight_failures.get(('|'.join(unit), target))
                if failure and self.now < failure['retry_at']:
                    reasons.append(f'{target}: waiting {int(failure["retry_at"] - self.now)} seconds after {failure["attempts"]} failed preflight checks. {failure["message"]}')
                    continue
                allowed, reason, overrides = self.policy.check(dict.fromkeys(unit, target), placements)
                if not allowed:
                    reasons.append(reason)
                    continue
                repair = self.policy.violations({**placements, **dict.fromkeys(unit, target)}) < self.policy.violations(placements)
                if repair and not self.config['placement_repair_enabled']:
                    reasons.append('Automatic placement repair is paused in DRS Settings.')
                    continue
                if broken and not repair or not repair and not any(placements[r] in self.hot for r in changing):
                    continue
                if repair and intent and intent['target'] in self.policy.domain(unit) and target != intent['target']:
                    reasons.append(f'Repair target remains {intent["target"]}; no rollback or alternate placement while that host is available.')
                    continue
                current_models = {**self.repair_models, **models} if repair else models
                move, reason = self.unit_move(unit, target, changing, current_models, placements, repair, overrides)
                if move:
                    candidates.append(move)
                elif reason:
                    reasons.append(f'{target}: {reason}')
            if candidates:
                occupied = {placements[r] for r in unit}
                candidates.sort(key=lambda m: (not m['rule_repair'],
                    m['target'] != intent['target'] if intent and m['rule_repair'] else False,
                    m['target'] not in occupied if m['rule_repair'] and len(unit) > 1 else False,
                    m['target_load'] if m['rule_repair'] else -m['score'], -m['target_free_memory'], m['target']))
                options.append(candidates[0])
            elif reasons:
                blocked.append({'resource': unit[0], 'source': placements[unit[0]], 'target': None,
                                'name': ' + '.join(self.samples.get(r, {}).get('metadata', {}).get('name') or r for r in unit),
                                'unit_members': unit, 'status': 'blocked', 'score': None, 'urgency': 'routine',
                                'reasons': sorted(set(reasons)), 'evaluation_track': 'placement_rules'})
        return options, blocked

    def unit_move(self, unit, target, changing, models, placements, repair, overrides):
        from . import balancing
        if self.infrastructure.get(target, {}).get('block_reason'):
            return None, self.infrastructure[target]['block_reason']
        if target not in models or not repair and models[target].get('repair_only'):
            return None, 'Target needs fresh CPU/RAM measurements.'
        simulated, moves, penalty = deepcopy(models), [], 0
        reserved_ram, reserved_cpu = 0, 0
        for resource in changing:
            kind = self.config.get('guest_kind', 'qemu')
            if resource.partition('/')[0] not in migration_groups.balancing_types(kind):
                return None, f'{resource}: this balancing job only migrates VMs.'
            source = placements[resource]
            sample, demand = self.samples.get(resource), self.profiles.get(resource)
            if source not in simulated or not balancing.fresh(sample, self.now):
                return None, f'{resource}: fresh guest and host CPU/RAM measurements are required.'
            if not repair and (simulated[source].get('repair_only') or not demand or demand['coverage'] < 1):
                return None, f'{resource}: complete current CPU/RAM measurements are required.'
            if any(sample.get(k) is None for k in ('mem_total', 'mem_host', 'cpu', 'cpu_count')) or not sample['mem_total'] or not sample['cpu_count'] or not repair and demand['peak_cores'] is None:
                return None, f'{resource}: CPU or resident RAM measurements are unavailable.'
            entries = {r['sid']: r for r in self.policy.native.get('ha_resources') or []}
            entry = entries.get(ha_guests.sid(resource))
            if entry or sample['metadata'].get('inventory', {}).get('ha', {}).get('managed'):
                if not self.config['ha_guests_enabled'] or not ha_guests.eligible(entry, self.policy.native.get('ha_rules'), ha_guests.sid(resource)):
                    return None, f'{resource}: native HA placement is unavailable or HA guest balancing is disabled.'
                peers = ha_guests.cohort(self.policy.native, ha_guests.sid(resource))
                if not peers.issubset(unit):
                    return None, f'{resource}: the complete native HA affinity group must move together.'
                limit = self.config['placement_concurrency']
                if self.config.get('migration_target_concurrency'):
                    limit = min(limit, self.config['migration_target_concurrency'])
                if len(peers.intersection(changing)) > limit:
                    return None, 'Native HA affinity group exceeds the configured parallel migration limit.'
            policy = migration_groups.automatic_migration_policy(self.policy.migration_groups, self.policy.nodes,
                sample['metadata']['inventory'], target, restart_containers=self.config['container_balancing_enabled'],
                stickiness=self.policies.get(resource, 5), intent='placement_rules' if repair else 'balance',
                exception_reason='Repair an enabled placement rule.' if repair else None)
            if not policy['allowed']:
                return None, f'{resource}: {policy["reason"]}'
            reason = storage.block_reason(self.checks.get(resource), source, target)
            if reason:
                return None, f'{resource}: {reason}'
            if not repair:
                reason = balancing.target_pressure(self.samples[f'node/{target}'], self.profiles[f'node/{target}'], self.now)
                if reason:
                    return None, reason
            failures = self.failures.get(resource, {})
            failure = failures.get('penalty', 0)
            if repair and failure:
                retry_at = failures['last_failed_at'] + min(300, 30 * 2 ** min(4, failures['failures_7d'] - 1))
                if self.now < retry_at:
                    return None, f'{resource}: waiting {int(retry_at - self.now)} seconds before retrying a failed rule repair.'
            penalty += 0 if repair else failure
            # Group moves keep cooldown and return protection; swap protection exempts critical
            # relief and maintenance, and rule repair is exempt from all three.
            if not repair and not self.aggressive:
                if demand['nodes'] != {source} or resource in self.recent_moves:
                    return None, f'{resource}: placement changed recently; group cooldown is active.'
                routine = self.hot.get(source, 'routine') == 'routine'
                churn = adaptive.movement_cost(self.movements.get(resource, []), target, self.now,
                                               adaptive.churn_policy(self.continuous, self.scheduled, self.adaptive),
                                               source if routine else None, self.flows)
                if churn['reason']:
                    return None, f'{resource}: {churn["reason"]}'
                if self.continuous:
                    penalty += churn['penalty']
            if repair:
                move, reason = placement_repair.member_move(sample, source, target, simulated, policy, self.workloads.get(resource))
            else:
                move, reason = balancing.candidate(sample, demand, source, target, simulated,
                    policy, self.hot.get(source, 'routine'), 'distribution', self.effort, self.workloads.get(resource), unit_member=True)
            if not move:
                return None, f'{resource}: {reason}'
            if move['requires_storage_review']:
                return None, f'{resource}: storage pressure requires review.'
            reserved_ram += move['reserved_memory']
            reserved_cpu += move['reserved_cpu_cores']
            simulated[source], simulated[target] = move['projection']['source_after'], move['projection']['target_after']
            moves.append(move)
        destination = models[target]
        reserve = 2 * balancing.GIB if repair else max(destination['ram'] * .1, 2 * balancing.GIB)
        if destination['memory'] * destination['ram'] + reserved_ram + reserve > destination['ram']:
            return None, 'Target cannot reserve RAM for the entire group plus host headroom.'
        cpu_limit = placement_repair.CPU_LIMIT if repair else .8
        if destination['cpu_live' if repair else 'cpu'] + reserved_cpu / destination['cores'] > cpu_limit:
            return None, f'Target cannot reserve current CPU demand for the entire group below {cpu_limit:.0%}.'
        if any(m.get('balloon_admission') for m in moves):
            from . import balloon
            room = balloon.available(destination, margin=True)
            if room is None or reserved_ram + destination.get('balloon_recoverable', 0) > room:
                return None, 'Target cannot reserve the entire group and existing balloon recovery.'
        terms = ('workload_selection', 'resource_relief', 'balance_improvement', 'commitment_relief',
                 'balloon_recovery', 'allocation_balance', 'distribution_gain', 'io_opportunity', 'contention_relief')
        score = 100 if repair else sum(sum(m['score_breakdown'][k] for k in terms) - m['score_breakdown']['migration_cost'] for m in moves) - penalty
        preference = self.policy.preference_gain(dict.fromkeys(unit, target), placements)
        if not repair:
            score += preference
        threshold = self.config['scheduled_minimum_score'] if self.scheduled else self.adaptive['minimum_score'] if self.continuous else .5
        if not repair and score <= threshold:
            return None, 'Moving the entire group does not justify its combined migration cost and history penalties.'
        for move in moves:
            move['placement_overrides'] = overrides
        first = moves[0]
        return {**first, 'name': ' + '.join(m['name'] for m in moves), 'score': 100 if repair else round(min(100, score), 2),
                'score_breakdown': {'placement_preference': round(preference, 2), 'group_net_benefit': round(score, 2), 'placement_repair': 100 if repair else 0, 'group_penalty': penalty},
                'unit_members': unit, 'unit_moves': moves, 'unit_key': '|'.join(unit), 'placement_policy_key': self.policy.key,
                'reserved_memory': reserved_ram, 'reserved_cpu_cores': reserved_cpu,
                'projection': {'source_before': models[first['source']], 'source_after': simulated[first['source']],
                               'target_before': models[target], 'target_after': simulated[target]},
                'placement_overrides': overrides, 'rule_repair': repair, 'unit_projection': simulated,
                'target_load': max(simulated[target]['cpu_live'] if repair else simulated[target]['cpu'], simulated[target]['memory']),
                'target_free_memory': simulated[target]['ram'] * (1 - simulated[target]['memory']),
                'evaluation_track': 'placement_rules', 'before': max(max(h['cpu'], h['memory']) for h in models.values()),
                'after': max(max(h['cpu'], h['memory']) for h in simulated.values()),
                'reason': 'Repair placement rules on one fixed target.' if repair else 'Move the together-group as one placement decision.',
                'requires_storage_review': False, 'policy_allowed': True}, None
