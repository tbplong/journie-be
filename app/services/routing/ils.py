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
"""

import random
import time
from dataclasses import dataclass, field

from app.services.routing.instance import Instance
from app.services.routing.validate import Tours

MAX_NO_IMPROVE = 150  # paper's stopping rule, used here as the restart rule
MIN_SHIFT = 1  # floored distances can make a detour 0 or negative; keep the ratio finite

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


def _refresh(inst: Instance, tour: _Tour) -> None:
    """Earliest-start schedule of the tour, then max_shift backwards."""
    nodes, travel = inst.nodes, inst.travel
    tour.start, tour.wait, closes = [], [], []
    time_now, end = inst.tours[tour.day]
    here = 0
    for node_id in tour.nodes:
        node = nodes[node_id]
        arrive = time_now + travel[here][node_id]
        start, close = node.start_at(arrive)  # tours are only ever built from feasible inserts
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
        if slot is not None:
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


def solve(inst: Instance, time_limit_s: float, seed: int = 0) -> tuple[Tours, Trace]:
    started = time.perf_counter()
    deadline = started + time_limit_s
    rng = random.Random(seed)
    candidates = {j for j in inst.customers if inst.nodes[j].score > 0}
    tours = [_Tour(day) for day in range(inst.m)]
    for tour in tours:
        _refresh(inst, tour)
    unvisited = set(candidates)
    _insert(inst, tours, unvisited, rng, deadline)
    best_nodes, best_score = [list(t.nodes) for t in tours], _score(inst, tours)
    trace: Trace = [(time.perf_counter() - started, best_score)]
    max_r = max(1, len(candidates) // (3 * inst.m))
    s, r, no_improve = 1, 1, 0
    while time.perf_counter() < deadline:
        _shake(inst, tours, unvisited, s, r)
        smallest = min(len(t.nodes) for t in tours)
        s += r
        r += 1
        if smallest and s >= smallest:
            s = max(1, s - smallest)
        if r >= max_r:
            r = 1
        _insert(inst, tours, unvisited, rng, deadline)
        score = _score(inst, tours)
        if score > best_score:
            best_nodes, best_score = [list(t.nodes) for t in tours], score
            trace.append((time.perf_counter() - started, score))
            r, no_improve = 1, 0
        else:
            no_improve += 1
        if no_improve >= MAX_NO_IMPROVE:  # restart from the best, at a random position
            for tour, nodes in zip(tours, best_nodes):
                tour.nodes = list(nodes)
                _refresh(inst, tour)
            unvisited = candidates - {n for nodes in best_nodes for n in nodes}
            s, r, no_improve = rng.randint(1, max(1, min(len(n) for n in best_nodes))), 1, 0
    return best_nodes, trace
