import itertools
import math
import random

from app.services.routing import ils
from app.services.routing.instance import Instance, Node
from app.services.routing.validate import tour_errors, validate


def random_instance(seed: int, customers: int = 6, m: int = 1) -> Instance:
    rng = random.Random(seed)
    nodes = [Node(0, 0, 0, 0, ((0, 0),))]
    for _ in range(customers):
        open_ = rng.randrange(0, 600)
        nodes.append(
            Node(
                rng.uniform(-50, 50),
                rng.uniform(-50, 50),
                rng.randrange(5, 40),
                rng.randrange(1, 30),
                ((open_, open_ + rng.randrange(20, 300)),),
            )
        )
    travel = tuple(
        tuple(math.floor(math.hypot(a.x - b.x, a.y - b.y)) for b in nodes) for a in nodes
    )
    return Instance(f"rand{seed}", tuple(nodes), travel, ((0, 700),) * m)


def brute_force_single_tour(inst: Instance) -> int:
    best = 0
    for size in range(1, len(inst.nodes)):
        for tour in itertools.permutations(inst.customers, size):
            if not tour_errors(inst, list(tour)):
                best = max(best, sum(inst.nodes[n].score for n in tour))
    return best


def test_ils_is_always_feasible_and_finds_small_optima() -> None:
    optimal = 0
    for seed in range(20):
        inst = random_instance(seed)
        tours, trace = ils.solve(inst, time_limit_s=0.2, seed=seed)
        check = validate(inst, tours)
        assert check.feasible, (seed, check.errors)
        assert trace[-1][1] == check.score  # the reported best is the returned solution
        optimal += check.score == brute_force_single_tour(inst)
    assert optimal >= 18, optimal


def test_ils_uses_every_tour_and_respects_tmax() -> None:
    inst = random_instance(99, customers=30, m=3)
    tours, _ = ils.solve(inst, time_limit_s=0.3, seed=1)
    assert validate(inst, tours).feasible
    assert len(tours) == 3  # one (possibly empty) tour per day, in day order


def test_insert_position_matches_a_full_reschedule() -> None:
    # The O(1) wait/max_shift test must agree with simulating the whole tour.
    for seed in range(30):
        inst = random_instance(seed, customers=8)
        tour = ils._Tour(0, nodes=[n for n in inst.customers if n % 2])
        while tour_errors(inst, tour.nodes):
            tour.nodes.pop()
        ils._refresh(inst, tour)
        for j in (n for n in inst.customers if n not in tour.nodes):
            fits = [
                p
                for p in range(len(tour.nodes) + 1)
                if not tour_errors(inst, tour.nodes[:p] + [j] + tour.nodes[p:])
            ]
            found = ils._best_position(inst, tour, j)
            assert (found is not None) == bool(fits), (seed, j)
            if found:
                assert found[1] in fits


def multi_window_instance(seed: int, customers: int = 7) -> Instance:
    """Two days with different hours; each node has two windows per day (a lunch-like gap)."""
    rng = random.Random(seed)
    days = ((0, 500), (1000, 1400))
    nodes = [Node(0, 0, 0, 0, ((0, 0),))]
    for _ in range(customers):
        windows = []
        for begin, _ in days:
            a = begin + rng.randrange(0, 150)
            b = a + rng.randrange(30, 120)
            c = b + rng.randrange(40, 120)
            windows += [(a, b), (c, c + rng.randrange(30, 150))]
        nodes.append(
            Node(
                rng.uniform(-40, 40),
                rng.uniform(-40, 40),
                rng.randrange(5, 30),
                rng.randrange(1, 30),
                tuple(windows),
            )
        )
    travel = tuple(
        tuple(math.floor(math.hypot(a.x - b.x, a.y - b.y)) for b in nodes) for a in nodes
    )
    return Instance(f"multi{seed}", tuple(nodes), travel, days)


def test_ils_handles_several_windows_and_day_specific_tours() -> None:
    for seed in range(20):
        inst = multi_window_instance(seed)
        tours, trace = ils.solve(inst, time_limit_s=0.2, seed=seed)
        check = validate(inst, tours)
        assert check.feasible, (seed, check.errors)
        assert len(tours) == 2 and trace[-1][1] == check.score


def test_multi_window_insert_test_is_sound() -> None:
    # Never accepts an infeasible insertion; may miss one that pushes a visit to a later window.
    for seed in range(30):
        inst = multi_window_instance(seed, customers=8)
        tour = ils._Tour(1, nodes=[n for n in inst.customers if n % 2])
        while tour_errors(inst, tour.nodes, 1):
            tour.nodes.pop()
        ils._refresh(inst, tour)
        for j in (n for n in inst.customers if n not in tour.nodes):
            found = ils._best_position(inst, tour, j)
            if found:
                trial = tour.nodes[: found[1]] + [j] + tour.nodes[found[1] :]
                assert not tour_errors(inst, trial, 1), (seed, j)
