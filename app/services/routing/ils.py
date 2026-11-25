"""Iterated Local Search for the TOPTW (eVansteenwgen, Souffriau, Vanden Berghe & Van Oudheusden,
"Iterated local search for the team orienteering problem with time windows", C&OR 36, 2009).

Insert step: repeatedly insert the unvisited node with the highest score^2 / shift, where shift
is the extra time its cheapest feasible position costs. Feasibility is O(1) per position from
each visit's ``wait`` and ``max_shift`` (how much later its service may start without leaving its
current window or making the tour late). With several windows per node this test is sound but
conservative: it never accepts an infeasible insertion, but it does not consider pushing a later
visit into its next window. Shake step: remove R consecutive visits from position S of every tour,
then S += R, R += 1 with the paper's wrap-arounds. Unlike the paper, which stops after
MAX_NO_IMPROVE shakes without improvement, the search restarts from the best solution (random
S, from ``seed``) until the time limit, so the 6 s budget is spent like the other solvers'.
With ``stall_shakes`` it stops after that many shakes without improvement instead: a count, not
a clock, so the same request gives the same plan however busy the server is.
"""

import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from app.services.routing.instance import Instance
from app.services.routing.validate import Tours, tour_errors

MAX_NO_IMPROVE = 150  # paper's stopping rule, used here as the restart rule
MIN_SHIFT = 1  # floored distances can make a detour 0 or negative; keep the ratio finite
EXACT_MAX = 9  # exact_order's table doubles with each visit; longer days keep the local search's
ORDER_STEPS = 60_000  # best_order's search per day: a few ms on a usual day, bounded on a long one

Trace = list[tuple[float, int]]  # (seconds since start, best score) at each improvement


@dataclass
class _Tour:
    day: int  # index into Instance.tours: begin and end time of this tour
    nodes: list[int] = field(default_factory=list)
    start: list[int] = field(default_factory=list)  # start of service per visit
    wait: list[int] = field(default_factory=list)
    max_shift: list[int] = field(default_factory=list)
    end_arrive: int = 0
    end_max_shift: int = 0


def _refresh(inst: Instance, tour: _Tour) -> bool:
    """Earliest-start schedule of the tour, then max_shift backwards. False (and the tour half
    filled) when the tour misses a window or the end of its day."""
    nodes, travel = inst.nodes, inst.travel
    tour.start, tour.wait, closes = [], [], []
    time_now, end = inst.tours[tour.day]
    here = 0
    for node_id in tour.nodes:
        node = nodes[node_id]
        arrive = time_now + travel[here][node_id]
        slot = node.start_at(arrive)
        if slot is None:
            return False
        start, close = slot
        tour.start.append(start)
        tour.wait.append(start - arrive)
        closes.append(close)
        time_now, here = start + node.service, node_id
    tour.end_arrive = time_now + travel[here][0]
    tour.end_max_shift = end - tour.end_arrive
    tour.max_shift = [0] * len(tour.nodes)
    after_wait, after_shift = 0, tour.end_max_shift
    for k in range(len(tour.nodes) - 1, -1, -1):
        tour.max_shift[k] = min(closes[k] - tour.start[k], after_wait + after_shift)
        after_wait, after_shift = tour.wait[k], tour.max_shift[k]
    return tour.end_max_shift >= 0


def _best_position(inst: Instance, tour: _Tour, j: int) -> tuple[int, int] | None:
    """(shift, position) of the cheapest feasible insertion of node j, or None."""
    nodes, travel = inst.nodes, inst.travel
    node = nodes[j]
    best: tuple[int, int] | None = None
    prev, depart = 0, inst.tours[tour.day][0]
    for p in range(len(tour.nodes) + 1):
        if p < len(tour.nodes):
            nxt, room = tour.nodes[p], tour.wait[p] + tour.max_shift[p]
        else:
            nxt, room = 0, tour.end_max_shift
        slot = node.start_at(depart + travel[prev][j])
        if slot is not None and not inst.clash(prev, j) and not inst.clash(j, nxt):
            start = slot[0]
            # travel to j + waiting at j + service at j + j->next, minus the arc it replaces
            shift = start - depart + node.service + travel[j][nxt] - travel[prev][nxt]
            if shift <= room and (best is None or shift < best[0]):
                best = (shift, p)
        if p < len(tour.nodes):
            prev = tour.nodes[p]
            depart = tour.start[p] + nodes[prev].service
    return best


def _insert(
    inst: Instance, tours: list[_Tour], unvisited: set[int], rng: random.Random, deadline: float
) -> None:
    """Greedy insertion until no unvisited node fits anywhere (or time is up)."""
    cache = [{j: _best_position(inst, tour, j) for j in unvisited} for tour in tours]
    while unvisited and time.perf_counter() < deadline:
        pick: tuple[float, float, int, int, int] | None = None  # ratio, tie, node, tour, pos
        for j in unvisited:
            options = [(c[j], r) for r, c in enumerate(cache) if c[j] is not None]
            if not options:
                continue
            (shift, pos), r = min(options, key=lambda o: o[0][0])
            ratio = inst.nodes[j].score ** 2 / max(shift, MIN_SHIFT)
            key = (ratio, rng.random(), j, r, pos)
            if pick is None or key > pick:
                pick = key
        if pick is None:
            return
        _, _, j, r, pos = pick
        tours[r].nodes.insert(pos, j)
        _refresh(inst, tours[r])
        unvisited.discard(j)
        for c in cache:
            c.pop(j, None)
        changed = cache[r]  # only tour r changed; the other tours' cached positions hold
        for k in unvisited:
            changed[k] = _best_position(inst, tours[r], k)


def _shake(inst: Instance, tours: list[_Tour], unvisited: set[int], s: int, r: int) -> None:
    """Remove r consecutive visits starting at 1-based position s of each tour (wrapping)."""
    for tour in tours:
        size = len(tour.nodes)
        if not size:
            continue
        drop = {(s - 1 + k) % size for k in range(min(r, size))}
        unvisited.update(tour.nodes[k] for k in drop)
        tour.nodes = [node for k, node in enumerate(tour.nodes) if k not in drop]
        _refresh(inst, tour)


def _score(inst: Instance, tours: list[_Tour]) -> int:
    return sum(inst.nodes[n].score for tour in tours for n in tour.nodes)


def solve(
    inst: Instance,
    time_limit_s: float,
    seed: int = 0,
    stall_shakes: int | None = None,
    initial: Tours | None = None,
) -> tuple[Tours, Trace]:
    """Stops at the time limit, when every node is visited (nothing left to gain), or, with
    ``stall_shakes``, after that many shakes without a better solution. ``initial`` is a
    feasible plan to start from (the result is never worse).

    Best = highest score, then fewest no_adjacent clashes. Inserting never creates a clash, but
    the shake can (removing B from A, B, C leaves A next to C)."""
    started = time.perf_counter()
    deadline = started + time_limit_s
    rng = random.Random(seed)
    candidates = {j for j in inst.customers if inst.nodes[j].score > 0}
    tours = [_Tour(day, list(initial[day]) if initial else []) for day in range(inst.m)]
    for tour in tours:
        _refresh(inst, tour)
    unvisited = candidates - {j for tour in tours for j in tour.nodes}
    _insert(inst, tours, unvisited, rng, deadline)
    best_nodes, best_score = [list(t.nodes) for t in tours], _score(inst, tours)
    best_clashes = inst.clashes(best_nodes)
    trace: Trace = [(time.perf_counter() - started, best_score)]
    max_r = max(1, len(candidates) // (3 * inst.m))
    s, r, no_improve, since_best = 1, 1, 0, 0
    total = sum(inst.nodes[j].score for j in candidates)
    while time.perf_counter() < deadline and (best_score < total or best_clashes):
        if stall_shakes is not None and since_best >= stall_shakes:
            break
        since_best += 1
        _shake(inst, tours, unvisited, s, r)
        smallest = min(len(t.nodes) for t in tours)
        s += r
        r += 1
        if smallest and s >= smallest:
            s = max(1, s - smallest)
        if r >= max_r:
            r = 1
        _insert(inst, tours, unvisited, rng, deadline)
        score, nodes = _score(inst, tours), [list(t.nodes) for t in tours]
        clashes = inst.clashes(nodes)
        if (score, -clashes) > (best_score, -best_clashes):
            best_nodes, best_score, best_clashes = nodes, score, clashes
            trace.append((time.perf_counter() - started, score))
            r, no_improve, since_best = 1, 0, 0
        else:
            no_improve += 1
        if no_improve >= MAX_NO_IMPROVE:  # restart from the best, at a random position
            for tour, nodes in zip(tours, best_nodes):
                tour.nodes = list(nodes)
                _refresh(inst, tour)
            unvisited = candidates - {n for nodes in best_nodes for n in nodes}
            s, r, no_improve = rng.randint(1, max(1, min(len(n) for n in best_nodes))), 1, 0
    return best_nodes, trace


def _built(inst: Instance, day: int, nodes: list[int]) -> _Tour | None:
    """The tour of these visits on this day, or None when it is infeasible."""
    tour = _Tour(day, nodes)
    return tour if _refresh(inst, tour) else None


def _positions(inst: Instance, tour: _Tour, j: int) -> Iterator[_Tour]:
    """The tour with j at every feasible position, checked in full. Unlike _best_position this
    also offers positions next to a visit of the same no_adjacent tag (the cost counts the
    clash: moving one restaurant beside another can be the step that frees a day) and pushes
    later visits into their next window."""
    for pos in range(len(tour.nodes) + 1):
        built = _built(inst, tour.day, tour.nodes[:pos] + [j] + tour.nodes[pos:])
        if built is not None:
            yield built


def _placed(inst: Instance, tour: _Tour, j: int) -> _Tour | None:
    found = _best_position(inst, tour, j)
    if found is None:
        return None
    pos = found[1]
    return _built(inst, tour.day, tour.nodes[:pos] + [j] + tour.nodes[pos:])


def _moves(inst: Instance, plan: list[_Tour], j: int) -> Iterator[dict[int, _Tour]]:
    """Changed tours of each neighbour that moves visit j: j to any feasible position in any
    tour (its own included, which reorders), then j swapped with a visit of another tour (each
    to its cheapest position)."""
    a = next(day for day, tour in enumerate(plan) if j in tour.nodes)
    rest = _built(inst, a, [n for n in plan[a].nodes if n != j])
    if rest is None:
        return
    for b in range(len(plan)):
        for moved in _positions(inst, rest if a == b else plan[b], j):
            yield {a: moved} if a == b else {a: rest, b: moved}
    for b in range(len(plan)):
        if b == a:
            continue
        for i in plan[b].nodes:
            rest_b = _built(inst, b, [n for n in plan[b].nodes if n != i])
            if rest_b is None:
                continue
            new_a, new_b = _placed(inst, rest, i), _placed(inst, rest_b, j)
            if new_a is not None and new_b is not None:
                yield {a: new_a, b: new_b}


def _spread(
    inst: Instance, plan: list[_Tour], cost: Callable[[Tours], tuple], best: tuple, deadline: float
) -> tuple[list[_Tour], tuple]:
    """Move visits from the fullest day to the emptiest one, the best such move each time, while
    that lowers ``cost``. The insert step fills the first days first; starting the full search
    from that plan leaves it in a poor local optimum (each day mixing far-apart areas)."""

    def fullness(tour: _Tour) -> float:
        begin, end = inst.tours[tour.day]
        return (tour.end_arrive - begin) / (end - begin) if tour.nodes else 0.0

    while True:
        days = [t for t in plan if inst.tours[t.day][1] > inst.tours[t.day][0]]
        if len(days) < 2:
            return plan, best
        full, empty = max(days, key=fullness), min(days, key=fullness)
        found: tuple[tuple, list[_Tour]] | None = None
        for j in full.nodes:
            rest = _built(inst, full.day, [n for n in full.nodes if n != j])
            if rest is None:
                continue
            for moved in _positions(inst, empty, j):
                if time.perf_counter() >= deadline:
                    return plan, best
                trial = [{full.day: rest, empty.day: moved}.get(t.day, t) for t in plan]
                value = cost([t.nodes for t in trial])
                if value < (found[0] if found else best):
                    found = (value, trial)
        if found is None:
            return plan, best
        best, plan = found


def rebalance(
    inst: Instance, tours: Tours, cost: Callable[[Tours], tuple], deadline: float
) -> Tours:
    """Local search on a feasible plan: first _spread, then visit by visit (in id order, round
    and round) take that visit's move that lowers ``cost`` of the whole plan the most. Every
    changed tour is re-checked, so the plan stays feasible; no visit is ever dropped. Stops at a
    local optimum (a full round without a better move) or at the deadline (perf_counter time).
    """
    plan = [_built(inst, day, list(nodes)) for day, nodes in enumerate(tours)]
    if any(tour is None for tour in plan):
        raise ValueError("rebalance needs a feasible plan")
    plan, best = _spread(inst, plan, cost, cost([t.nodes for t in plan]), deadline)
    order = sorted(j for tour in plan for j in tour.nodes)
    stale, k = 0, 0
    while stale < len(order):
        j, k = order[k % len(order)], k + 1
        found: tuple[tuple, list[_Tour]] | None = None
        for changed in _moves(inst, plan, j):
            if time.perf_counter() >= deadline:
                return [t.nodes for t in plan]
            trial = [changed.get(day, tour) for day, tour in enumerate(plan)]
            value = cost([t.nodes for t in trial])
            if value < (found[0] if found else best):
                found = (value, trial)
        if found is None:
            stale += 1
        else:
            best, plan, stale = found[0], found[1], 0
    return [t.nodes for t in plan]


def exact_order(inst: Instance, day: int, nodes: list[int]) -> list[int] | None:
    """An order of these visits that fits the day, or None when no order does: dynamic
    programming over (visited set, last visit), keeping the earliest finish per state, which is
    exact because leaving earlier never closes a window."""
    if not nodes:
        return []
    begin, end = inst.tours[day]
    best: dict[tuple[int, int], tuple[int, int]] = {}  # (mask, last index) -> (leave, previous)

    def extend(mask: int, k: int, here: int, moment: int, previous: int) -> None:
        j = nodes[k]
        slot = inst.nodes[j].start_at(moment + inst.travel[here][j])
        if slot is None:
            return
        leave = slot[0] + inst.nodes[j].service
        key = (mask | 1 << k, k)
        if leave + inst.travel[j][0] <= end and (key not in best or leave < best[key][0]):
            best[key] = (leave, previous)

    size = len(nodes)
    for k in range(size):
        extend(0, k, 0, begin, -1)
    for mask in range(1, 1 << size):
        for last in range(size):
            if (mask, last) in best:
                for k in range(size):
                    if not mask >> k & 1:
                        extend(mask, k, nodes[last], best[mask, last][0], last)
    full = (1 << size) - 1
    ends = [k for k in range(size) if (full, k) in best]
    if not ends:
        return None
    k = min(ends, key=lambda e: best[full, e][0] + inst.travel[nodes[e]][0])
    order, mask = [], full
    while k >= 0:
        order.append(nodes[k])
        k, mask = best[mask, k][1], mask ^ (1 << k)
    return order[::-1]


def best_order(
    inst: Instance,
    day: int,
    nodes: list[int],
    clash: Callable[[int, int], bool],
    penalty: Callable[[int, int, int, bool, int], tuple[int, int]],
) -> list[int] | None:
    """The order of these visits with the fewest ``clash`` pairs, then the least lost time:
    travel plus ``penalty(node, arrival, start, first visit, state) -> (lost, state)`` for each
    visit, which counts its waiting and the soft rules; ``state`` starts at 0 and is carried
    along the day. Lost time is never negative, so the bound holds. Branch and bound from the
    given order. ``ORDER_STEPS`` is a count, so the same day always gets the same answer."""
    begin, end = inst.tours[day]
    best: tuple[tuple[int, int], list[int]] | None = None
    steps = 0
    order: list[int] = []

    def walk(here: int, moment: int, clashes: int, lost: int, left: list[int], state: int) -> None:
        nonlocal best, steps
        if not left:
            rank = (clashes, lost + inst.travel[here][0])
            if best is None or rank < best[0]:
                best = (rank, list(order))
            return
        for j in left:
            steps += 1
            if steps > ORDER_STEPS:
                return
            arrive = moment + inst.travel[here][j]
            slot = inst.nodes[j].start_at(arrive)
            if slot is None:
                continue
            leave = slot[0] + inst.nodes[j].service
            if leave + inst.travel[j][0] > end:
                continue
            delta, after = penalty(j, arrive, slot[0], not order, state)
            spent = lost + inst.travel[here][j] + delta
            pairs = clashes + (bool(order) and clash(here, j))
            if best is not None and (pairs, spent) >= best[0]:
                continue
            order.append(j)
            walk(j, leave, pairs, spent, [n for n in left if n != j], after)
            order.pop()

    walk(0, begin, 0, 0, list(nodes), 0)
    return best[1] if best else None


def polish(
    inst: Instance,
    tours: Tours,
    cost: Callable[[Tours], tuple],
    deadline: float,
    clash: Callable[[int, int], bool],
    penalty: Callable[[int, int, int, bool, int], tuple[int, int]],
) -> Tours:
    """Put each day in its best order: moving one visit at a time can stop with a far place in
    the wrong spot. A day changes only when the whole plan's ``cost`` drops."""
    plan = [list(tour) for tour in tours]
    best = cost(plan)
    for day, tour in enumerate(plan):
        if not 3 <= len(tour) <= EXACT_MAX or time.perf_counter() >= deadline:
            continue
        order = best_order(inst, day, tour, clash, penalty)
        if order is None or order == tour:
            continue
        trial = [order if d == day else t for d, t in enumerate(plan)]
        value = cost(trial)
        if value < best:
            best, plan = value, trial
    return plan


def fill(inst: Instance, tours: Tours, cost: Callable[[Tours], tuple], deadline: float) -> Tours:
    """Insert the nodes a feasible plan leaves out, highest score first, each at the feasible
    position (any tour) with the lowest ``cost``. After rebalancing, a day can have room the
    insert step never saw: it only tries the cheapest position, conservatively. A node that fits
    no position may still fit once its day is put in another order."""
    plan = [list(tour) for tour in tours]
    visited = {j for tour in plan for j in tour}
    left = [j for j in inst.customers if j not in visited and inst.nodes[j].score > 0]
    for j in sorted(left, key=lambda j: (-inst.nodes[j].score, j)):
        best: tuple[tuple, Tours] | None = None
        for day, tour in enumerate(plan):
            for pos in range(len(tour) + 1):
                if time.perf_counter() >= deadline:
                    return plan
                trial = tour[:pos] + [j] + tour[pos:]
                if tour_errors(inst, trial, day):
                    continue
                candidate = [trial if d == day else t for d, t in enumerate(plan)]
                value = cost(candidate)
                if best is None or value < best[0]:
                    best = (value, candidate)
        if best is None:
            for day, tour in enumerate(plan):
                if len(tour) >= EXACT_MAX or time.perf_counter() >= deadline:
                    continue
                order = exact_order(inst, day, [*tour, j])
                if order is None:
                    continue
                candidate = [order if d == day else t for d, t in enumerate(plan)]
                value = cost(candidate)
                if best is None or value < best[0]:
                    best = (value, candidate)
        if best is not None:
            plan = best[1]
    return plan
