import heapq
import math
import time
from fractions import Fraction

from . import balancing, ha_guests, placement_rules, storage, telemetry

GIB = 1024 ** 3
# Each simulated host failure has a time budget; the state cap is only a safety net.
SEARCH_SECONDS = 2.0
SEARCH_LIMIT = 1000000
SEARCH_DEPTH = 200
# Search phases as (discrepancy limit, share of the remaining budget): a short depth-first
# search, a limited-discrepancy probe that revisits early choices, then the exhaustive search.
PHASES = ((None, .1), (1, .15), (None, 1))
MEMO_LIMIT = 100000
HOST_SETS = 32
THRESHOLDS = 16
# Grid steps per host for the subset-sum reduction, once per scenario and at each search state.
GRID_STEPS = 1 << 16
SEARCH_STEPS = 1 << 12
EPSILON = 1e-6
DELTA = 1e-9
DIMENSIONS = (('ram', 'memory_free'), ('cpu', 'cpu_free'))


def _room(unit, room):
    # Budgets shrink by repeated float subtraction, so an exact fit must not depend on the
    # order of earlier placements; tolerate a relative rounding error of DELTA.
    return (room['memory_free'] >= unit['ram'] - DELTA * max(1, unit['ram'])
            and room['cpu_free'] >= unit['cpu'] - DELTA * max(1, unit['cpu']))


def _fill(items, capacity):
    """Fractional knapsack bound on guests for (weight, guests) items sorted by weight per guest."""
    placed = 0
    for weight, guests in items:
        if weight > capacity:
            return int(placed + guests * max(capacity, 0) / weight + EPSILON)
        capacity -= weight
        placed += guests
    return placed


def _unit(values):
    """Largest unit that divides every value exactly, or 0 when it is too fine to matter."""
    unit, top = Fraction(0), 0
    for value in values:
        top = max(top, value)
        value = Fraction(value)
        unit = Fraction(math.gcd(unit.numerator * value.denominator, value.numerator * unit.denominator),
                        unit.denominator * value.denominator)
    return float(unit) if unit and unit * 2 ** 40 >= top else 0


def _grid(exact, top, steps):
    """The exact common unit when a host spans at most `steps` of it, otherwise a rounded grid."""
    if exact and top <= exact * steps:
        return exact
    return top / steps if top > 0 else 0


def _items(units, budgets):
    """Units as (guests, sizes, fitting hosts), without units that fit on no host."""
    items = []
    for unit in units:
        hosts = frozenset(n for n in unit['targets'] if _room(unit, budgets[n]))
        if hosts:
            items.append((len(unit['members']), (unit['ram'], unit['cpu']), hosts))
    return items


def _rooms(items, budgets, exact=(0, 0)):
    """Free capacity of the hosts these items fit on; sizes in multiples of `exact` cannot use a remainder."""
    hosts = {n for *_, h in items for n in h}
    return {n: tuple(math.floor(budgets[n][free] / unit + DELTA) * unit if unit else budgets[n][free]
                     for (_, free), unit in zip(DIMENSIONS, exact)) for n in hosts}


def _limit(items, rooms, need=0):
    """Pooled and per-host fractional knapsack bounds on the guests of these items."""
    if not items:
        return 0
    limit = sum(g for g, _, _ in items)
    ranked = [sorted(items, key=lambda i: i[1][d] / i[0]) for d in range(len(DIMENSIONS))]
    for d, group in enumerate(ranked):
        limit = min(limit, _fill([(s[d], g) for g, s, _ in group], sum(r[d] for r in rooms.values())))
        if limit < need:
            return limit
    per_host = sum(min(_fill([(s[d], g) for g, s, h in group if node in h], room[d]) for d, group in enumerate(ranked))
                   for node, room in rooms.items())
    return min(limit, per_host)


def _bound(units, budgets, exact=(0, 0)):
    """Admissible upper bound on the guests of these units that can still be placed."""
    items = _items(units, budgets)
    return _limit(items, _rooms(items, budgets, exact))


def _reduce(items, rooms, grid):
    """Relaxed copy in grid steps, with each host's capacity cut to an achievable sum of sizes."""
    steps = [(g, tuple(math.floor(x / unit + DELTA) if unit else x for x, unit in zip(s, grid)), h) for g, s, h in items]
    reduced = {}
    for node, room in rooms.items():
        sizes = [s for _, s, h in steps if node in h]
        capacity = list(room)
        for d, unit in enumerate(grid):
            if unit:
                # Rounding may lift each size by DELTA steps; the capacity allows for all of them.
                top = max(0, math.floor(room[d] / unit + DELTA * (len(sizes) + 1)))
                reach, full = 1, (1 << top + 1) - 1
                for size in sizes:
                    reach |= (reach << size[d]) & full
                capacity[d] = reach.bit_length() - 1
        reduced[node] = tuple(capacity)
    return steps, reduced


def _shares(items, rooms):
    """Martello-Toth L2 bound per dimension, using dual feasible functions."""
    hosts = len({n for *_, h in items for n in h})
    limit = sum(g for g, _, _ in items)
    for d in range(len(DIMENSIONS)):
        sizes = [(s[d] / max(rooms[n][d] for n in h) if s[d] > 0 else 0, g) for g, s, h in items]
        cuts = sorted({x for x, _ in sizes if 3 * DELTA < x <= .5})
        for t in [x - 2 * DELTA for x in cuts[::max(1, -(-len(cuts) // THRESHOLDS))]] + [.5]:
            shares = [(1 if x > 1 - t + DELTA else 0 if x < t + DELTA else x, g) for x, g in sizes]
            limit = min(limit, _fill(sorted(shares, key=lambda s: s[0] / s[1]), hosts))
    return limit


def _ceiling(units, budgets, exact, deadline):
    """Stronger upper bound over host sets, in the style of Hall's condition."""
    items = _items(units, budgets)
    if not items:
        return 0
    total = sum(g for g, _, _ in items)
    rooms = _rooms(items, budgets)
    grid = [_grid(unit, max(r[d] for r in rooms.values()), GRID_STEPS) for d, unit in enumerate(exact)]
    versions = [(items, _rooms(items, budgets, exact)), _reduce(items, rooms, grid)]
    groups = sorted({h for *_, h in items}, key=sorted)
    sets = [frozenset()]
    for group in groups:
        sets += [s | group for s in sets if not group <= s and s | group not in sets]
        if len(sets) > HOST_SETS:
            sets = groups + [frozenset().union(*groups)]
            break
    limit = total
    for hosts in sorted({s for s in sets if s}, key=lambda s: (-len(s), sorted(s))):
        if time.monotonic() > deadline:
            break
        for group, room in versions:
            inside = [i for i in group if i[2] <= hosts]
            outside = total - sum(g for g, _, _ in inside)
            if inside and outside < limit:
                local = {n: room[n] for n in {n for *_, h in inside for n in h}}
                limit = min(limit, outside + _limit(inside, local), outside + _shares(inside, local))
    return limit


def pack(units, budgets, check, base, cost=None, search_limit=SEARCH_LIMIT, seconds=SEARCH_SECONDS, stats=None):
    """Place as many guests as possible within a time budget."""
    started = time.monotonic()
    deadline = started + seconds
    initial = {n: dict(h) for n, h in budgets.items()}
    budgets = {n: dict(h) for n, h in budgets.items()}
    units = [u for u in units if u['targets']]
    exact = [_unit(u[key] for u in units) for key, _ in DIMENSIONS]
    ceiling = _bound(units, budgets, exact)
    allocations, exceptions = {}, []
    visited, stopped, until, cap = 0, None, deadline, search_limit

    def options(unit, placements):
        found = []
        for target in unit['targets']:
            room = budgets[target]
            if not _room(unit, room):
                continue
            changes = dict.fromkeys(unit['members'], target)
            allowed, _, extra = check(changes, placements) if unit['ruled'] else (True, None, [])
            if allowed:
                value = cost({**placements, **changes}) if cost and unit['preferred'] else 0
                found.append((len(extra), value, -room['memory_free'], target, changes, extra))
        # Compliant hosts first, then Should preferences, then the most free memory.
        return sorted(found, key=lambda o: o[:4])

    def ordered(sequence):
        placements = dict(base)
        for unit in sequence:
            choice = next(iter(options(unit, placements)), None)
            if choice:
                *_, target, changes, extra = choice
                budgets[target]['memory_free'] -= unit['ram']
                budgets[target]['cpu_free'] -= unit['cpu']
                allocations.update(changes)
                exceptions.extend(extra)
                placements.update(changes)
        result = dict(allocations), list(exceptions), cost(placements) if cost else 0
        for node, room in initial.items():
            budgets[node].update(room)
        allocations.clear()
        exceptions.clear()
        return result

    def finish(complete):
        if stats is not None:
            elapsed = time.monotonic() - started
            stats.update({'states': visited, 'state_limit': search_limit, 'seconds': round(elapsed, 3),
                          'budget_seconds': seconds, 'budget_used': round(min(elapsed / seconds, 1), 3) if seconds > 0 else 1,
                          'stopped_by': None if complete else stopped})
        return best, best_exceptions, complete, ceiling

    # Ordered passes spread guests and usually reach the bound without backtracking:
    # largest guests first for a complete placement, smallest first to restart the most guests.
    best, best_exceptions, best_cost = ordered(units)
    if len(best) < ceiling:
        alternative = ordered(sorted(units, key=lambda u: (u['ram'] / len(u['members']), u['cpu'] / len(u['members']))))
        if len(alternative[0]) > len(best):
            best, best_exceptions, best_cost = alternative
    if len(best) < ceiling:
        ceiling = min(ceiling, _ceiling(units, budgets, exact, deadline))
    if len(best) >= ceiling or len(units) > SEARCH_DEPTH:
        # Large scenarios keep the ordered passes to bound recursion depth and time.
        stopped = 'units'
        return finish(len(best) >= ceiling)

    # Fewest permitted hosts first, then largest in the tighter dimension. Plain units that may use
    # every host come last, where equivalent hosts are interchangeable. Units with rules or
    # preferences keep their relative order, so rule checks see the same earlier placements.
    tight = [sum(u[key] for u in units) / max(sum(max(r[free], 0) for r in budgets.values()), EPSILON) for key, free in DIMENSIONS]
    major, minor = sorted(range(len(DIMENSIONS)), key=lambda d: -tight[d])
    (major_key, major_free), (minor_key, minor_free) = DIMENSIONS[major], DIMENSIONS[minor]
    widest = max(len(u['targets']) for u in units)

    def rank(u):
        flexible = u['plain'] and len(u['targets']) == widest
        return flexible, len(u['targets']), -u[major_key], u['targets'], -len(u['members']), -u[minor_key], u['members']

    order = list(heapq.merge([u for u in units if not u['plain']], sorted((u for u in units if u['plain']), key=rank), key=rank))
    count, hosts = len(order), sorted(budgets)
    # From this index on only plain units remain, so equivalent hosts are interchangeable.
    plain_from = max((i + 1 for i, u in enumerate(order) if not u['plain']), default=0)
    position = {n: i for i, n in enumerate(hosts)}
    targets = [sorted(u['targets'], key=position.get) for u in order]
    masks = {n: sum(1 << i for i, u in enumerate(order) if n in u['targets']) for n in hosts}
    demand = {n: [[0] * (count + 1) for _ in DIMENSIONS] for n in hosts}
    for i in range(count - 1, -1, -1):
        for n in hosts:
            for d, (key, _) in enumerate(DIMENSIONS):
                demand[n][d][i] = demand[n][d][i + 1] + (order[i][key] if n in order[i]['targets'] else 0)
    alike = [i + 1 < count and order[i]['plain'] and order[i + 1]['plain'] and order[i]['targets'] == order[i + 1]['targets']
             and len(order[i]['members']) == len(order[i + 1]['members']) for i in range(count)]
    remaining = [sum(len(u['members']) for u in order[i:]) for i in range(count + 1)]
    memo = {}
    grid = [_grid(unit, max(max(r[free], 0) for r in budgets.values()), SEARCH_STEPS) for unit, (_, free) in zip(exact, DIMENSIONS)]
    reductions = [0, 0]

    def bound(index, need):
        items = _items(order[index:], budgets)
        limit = _limit(items, _rooms(items, budgets, exact), need)
        # The subset-sum reduction runs while it keeps pruning a useful share of states.
        if items and limit >= need and (reductions[0] < 256 or reductions[1] * 16 >= reductions[0]):
            limit = min(limit, _limit(*_reduce(items, _rooms(items, budgets), grid), need))
            reductions[0] += 1
            reductions[1] += limit < need
        return limit

    def slack(node, d, index):
        # A dimension cannot constrain a host that holds all remaining demand that may use it.
        return demand[node][d][index] * (1 + DELTA) <= budgets[node][DIMENSIONS[d][1]]

    def signature(node, index):
        mask = masks[node] >> index
        if not mask:
            return None
        return (mask, *(math.inf if slack(node, d, index) else budgets[node][free] for d, (_, free) in enumerate(DIMENSIONS)))

    def search(index, placements, value, trail, chain, budget):
        nonlocal best, best_exceptions, best_cost, visited, stopped
        if visited >= cap or time.monotonic() > until:
            stopped = 'states' if visited >= cap else 'time'
            return
        visited += 1
        if len(allocations) > len(best) or len(allocations) == len(best) and value < best_cost:
            best, best_exceptions, best_cost = dict(allocations), list(exceptions), value
        # Only branches that can still reach the goal are explored.
        level = max(len(best), goal - 1)
        if len(best) >= ceiling or len(allocations) + remaining[index] <= level or len(allocations) + bound(index, level - len(allocations) + 1) <= level:
            return
        unit = order[index]
        if not unit['plain']:
            choices = [(target, changes, extra) for *_, target, changes, extra in options(unit, placements)]
        elif chain and chain[2] is None:
            # Interchangeable guests are skipped only after their predecessor.
            choices = []
        else:
            symmetric = index >= plain_from
            if not chain:
                # The remaining problem depends only on free capacity and earlier rule placements;
                # with plain units only, hosts of equal eligibility and room are interchangeable.
                rooms = [signature(n, index) for n in hosts]
                state = (index, trail, tuple(sorted(filter(None, rooms))) if symmetric else tuple(rooms))
                seen = memo.get(state)
                if seen and seen[0] >= len(allocations) and (seen[1] is None or budget is not None and seen[1] >= budget):
                    return
                if state in memo or len(memo) < MEMO_LIMIT:
                    memo[state] = (len(allocations), budget)
            classes, choices = set(), []
            for target in targets[index]:
                if chain and position[target] < chain[2] or not _room(unit, budgets[target]):
                    continue
                kind = signature(target, index) if symmetric else target
                if kind not in classes:
                    classes.add(kind)
                    choices.append(target)
            # Most room in the tighter dimension first, as in greedy multiway partitioning: loads
            # stay balanced and the remaining smaller guests fill the gaps.
            choices.sort(key=lambda t: (-budgets[t][major_free], -budgets[t][minor_free], position[t]))
            choices = [(t, dict.fromkeys(unit['members'], t), []) for t in choices]
        link = None
        if alike[index]:
            # Identical guests use non-decreasing host order. Dimensions that cannot constrain
            # any eligible host since the first guest of the chain may differ.
            head, free = chain[:2] if chain else (index, [all(slack(n, d, index) for n in unit['targets']) for d in range(len(DIMENSIONS))])
            if all(free[d] or order[index + 1][key] == order[head][key] for d, (key, _) in enumerate(DIMENSIONS)):
                link = head, free
        for turn, (target, changes, extra) in enumerate(choices + [(None, {}, [])]):
            # Any choice after the preferred one is a discrepancy.
            spend = min(turn, 1)
            if budget is not None and spend > budget:
                break
            following = None if budget is None else budget - spend
            child = link and (*link, None if target is None else position[target])
            if target is None:
                if len(allocations) + remaining[index + 1] > level:
                    search(index + 1, placements, value, trail if unit['plain'] else (*trail, None), child, following)
            else:
                room = budgets[target]
                saved = dict(room)
                room['memory_free'] -= unit['ram']
                room['cpu_free'] -= unit['cpu']
                allocations.update(changes)
                exceptions.extend(extra)
                if unit['plain']:
                    search(index + 1, placements, value, trail, child, following)
                else:
                    moved = {**placements, **changes}
                    search(index + 1, moved, cost(moved) if cost else 0, (*trail, target), child, following)
                room.update(saved)
                for key in changes:
                    del allocations[key]
                del exceptions[len(exceptions) - len(extra):]
            if len(best) >= ceiling or stopped:
                return

    # Each search only looks for placements that reach the ceiling. The probe follows the preferred
    # host order with at most one deviation, so an early wrong choice does not hide a packing.
    # An exhaustive search that finds none proves the ceiling one guest lower.
    goal = ceiling
    for budget, share in PHASES:
        until = time.monotonic() + share * max(deadline - time.monotonic(), 0)
        cap = visited + int(share * max(search_limit - visited, 0))
        while len(best) < ceiling:
            memo.clear()
            stopped, goal = None, ceiling
            search(0, dict(base), cost(base) if cost else 0, (), None, budget)
            if budget is not None or stopped:
                break
            if len(best) < goal:
                ceiling = goal - 1
    return finish(len(best) >= ceiling)


def classify(scenarios, unassessed=()):
    """Overall status shared by the one-time report and the background monitor."""
    def hosts(test):
        return sorted(s['failed_host'] for s in scenarios if test(s))

    failed = hosts(lambda s: s['status'] == 'insufficient')
    excepted = hosts(lambda s: s['status'] == 'rule_exceptions' or s['status'] == 'fits' and s.get('exceptions'))
    uncertain = hosts(lambda s: s['status'] not in ('fits', 'insufficient', 'rule_exceptions'))
    incomplete = bool(uncertain or unassessed or not scenarios)
    return {'status': 'at_risk' if failed or excepted else 'unknown' if incomplete else 'fits', 'incomplete': incomplete,
            'failed_hosts': failed, 'exception_hosts': excepted, 'uncertain_hosts': uncertain,
            'overcommitted_hosts': sorted({n for s in scenarios if s['status'] == 'insufficient' for n in s.get('overcommitted_hosts', [])})}


def evaluate(db, connection_id, resources, native, now, memory_mode='configured', search_limit=SEARCH_LIMIT, seconds=SEARCH_SECONDS):
    samples = {s['resource']: s for s in telemetry.latest(db, connection_id, now)}
    policy = placement_rules.Policy(db, connection_id, resources, native=native)
    nodes = {r['node']: r for r in resources if r.get('type') == 'node'}
    guests = {r['id']: r for r in resources if r.get('type') in ('qemu', 'lxc') and r.get('status') == 'running'}
    unassessed = [{'resource': k, 'name': g.get('name') or k, 'node': g['node']} for k, g in guests.items() if g['node'] not in policy.available]
    checks, speed = storage.checks(db, connection_id, now), balancing.performance(db, connection_id)
    demand, unknown = {}, {}
    for key, guest in guests.items():
        s = samples.get(key)
        if not s or s['node'] != guest['node'] or not balancing.fresh(s, now) or any(s.get(k) is None for k in ('mem_total', 'mem_host', 'cpu', 'cpu_count')) or not s['mem_total'] or not s['cpu_count']:
            unknown[key] = 'Fresh guest RAM and CPU measurements are missing.'
            continue
        ram = max(s['mem_total'], s['mem_host']) if memory_mode == 'configured' else max(s['mem_host'], (s['mem_used'] or 0) * 1.2)
        demand[key] = {'ram': ram, 'cpu': max(s['cpu'], .1) * s['cpu_count'] * speed.get(guest['node'], 1), 'resident': s['mem_host']}
    hosts, host_errors = {}, {}
    for node in sorted(policy.available):
        s = samples.get(f'node/{node}')
        resident = [k for k, g in guests.items() if g['node'] == node]
        if not s or not balancing.fresh(s, now) or any(s.get(k) is None for k in ('mem_total', 'mem_used', 'cpu', 'cpu_count')) or not s['mem_total'] or not s['cpu_count'] or any(k in unknown for k in resident):
            host_errors[node] = 'Host capacity or resident guest measurements are incomplete.'
            continue
        overhead = max(0, s['mem_used'] - sum(demand[k]['resident'] for k in resident))
        committed = overhead + sum(demand[k]['ram'] for k in resident)
        cpu = max(s['cpu'] * s['cpu_count'] * speed.get(node, 1), sum(demand[k]['cpu'] for k in resident))
        hosts[node] = {'memory_free': s['mem_total'] - max(2 * GIB, s['mem_total'] * .1) - committed,
                       'cpu_free': .9 * s['cpu_count'] * speed.get(node, 1) - cpu,
                       'memory_total': s['mem_total'], 'cpu_total': s['cpu_count'] * speed.get(node, 1)}
    scenarios = []
    for failed in sorted(n for n, r in nodes.items() if r.get('status') == 'online' and n in policy.available):
        scenario_resources = [{**r, 'status': 'offline'} if r.get('type') == 'node' and r['node'] == failed else r for r in resources]
        failure_policy = placement_rules.Policy(db, connection_id, scenario_resources, native=native)
        affected = {k for k, g in guests.items() if g['node'] == failed}
        units = placement_rules.components(failure_policy.rules, affected)
        budgets = {n: dict(h) for n, h in hosts.items() if n != failed}
        overcommitted = {n: 'Surviving host already exceeds the reserved RAM/CPU capacity before any failure.'
                         for n, h in budgets.items() if h['memory_free'] < 0 or h['cpu_free'] < 0}
        ruled = {g for rule in failure_policy.rules for g in rule['guests']}
        preferred = {g for rule in failure_policy.preferences for g in rule['guests']}
        constraints, work = {}, []
        for unit in units:
            options, reasons = [], []
            ram = sum(demand[k]['ram'] for k in unit if k in demand)
            cpu = sum(demand[k]['cpu'] for k in unit if k in demand)
            for target in sorted(failure_policy.available):
                issues = [unknown[k] for k in unit if k in unknown]
                if target not in budgets:
                    issues.append(host_errors.get(target, 'Target capacity is unknown.'))
                if target not in failure_policy.domain(unit):
                    issues.append('Target is outside the compatible migration domain or required host set.')
                for key in unit:
                    issue = storage.block_reason(checks.get(key), failed, target)
                    if issue:
                        issues.append(issue)
                # A host failure is not maintenance: Must rules keep only their availability exceptions.
                allowed, reason, _ = failure_policy.check(dict.fromkeys(unit, target))
                if not allowed:
                    issues.append(reason)
                if issues:
                    reasons.append({'target': target, 'reasons': sorted(set(issues))})
                else:
                    options.append(target)
            constraints.update({k: reasons for k in unit})
            flags = {'ruled': bool(ruled.intersection(unit)), 'preferred': bool(preferred.intersection(unit))}
            work.append({'members': unit, 'targets': options, 'ram': ram, 'cpu': cpu, **flags, 'plain': not any(flags.values())})
        work.sort(key=lambda u: (len(u['targets']), -u['ram'], -u['cpu'], not u['plain'], u['targets'], u['members']))
        cost = failure_policy.preference_cost if preferred & affected else None
        search = {}
        best, exceptions, complete, ceiling = pack(work, budgets, failure_policy.check, failure_policy.placements, cost, search_limit, seconds, search)
        missing = affected - best.keys()
        # A shortfall is proven by an exhaustive search or by an upper bound below the affected count.
        proven = complete or ceiling < len(affected)
        unplaced = [{'resource': k, 'name': guests[k].get('name') or k,
                     'reason': unknown.get(k) or ('No permitted target has verified storage and capacity.' if not next(u['targets'] for u in work if k in u['members']) else 'No complete packing found within the search limit.' if not proven else 'Insufficient remaining RAM/CPU or conflicting placement requirements.'),
                     'targets': constraints[k]} for k in sorted(missing)]
        uncertainty = bool(host_errors or any(k in unknown or not checks.get(k) or checks[k]['state'] == 'unknown' or checks[k]['node'] != failed for k in affected))
        for key, target in best.items():
            budgets[target]['memory_free'] -= demand[key]['ram']
            budgets[target]['cpu_free'] -= demand[key]['cpu']
        status = ('unknown' if uncertainty else 'insufficient' if overcommitted or missing and proven else
                  'search_limited' if missing else 'rule_exceptions' if exceptions else 'fits')
        scenarios.append({'failed_host': failed, 'status': status,
                          'affected': len(affected), 'placed': len(best), 'unplaced': unplaced,
                          'placements': [{'resource': k, 'name': guests[k].get('name') or k, 'target': n} for k, n in sorted(best.items())],
                          'exceptions': sorted(set(exceptions)), 'host_errors': {**host_errors, **overcommitted},
                          'overcommitted_hosts': sorted(overcommitted), 'search_complete': complete,
                          'search': search, 'surviving_capacity': budgets})
    return {'evaluated_at': now, 'memory_mode': memory_mode, 'status': classify(scenarios, unassessed)['status'],
            'scenarios': scenarios, 'unassessed_guests': unassessed, 'running_guests': len(guests), 'ha_managed': len([k for k in guests if ha_guests.sid(k) in {r['sid'] for r in native.get('ha_resources', [])}]),
            'note': 'Capacity simulation only. It does not enable HA or restart guests. CPU uses current demand with a 10% vCPU floor; hosts retain 10% CPU and at least 10% RAM / 2 GiB headroom. Current placements on surviving hosts stay fixed. Must and native HA rules stay hard constraints apart from their availability exceptions. Observed RAM is an estimate; configured RAM is the conservative mode. Local storage and unknown data cannot establish restartability.'}
