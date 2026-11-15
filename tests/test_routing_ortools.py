import pytest

from app.services.routing.validate import validate
from tests.test_routing_ils import multi_window_instance, random_instance

pytest.importorskip("ortools", reason="install the bench group: uv sync --group bench")
from bench.routing import ortools_solver


def test_ortools_solutions_are_feasible_with_several_windows_and_days() -> None:
    for seed in range(5):
        inst = multi_window_instance(seed)
        tours, trace = ortools_solver.solve(inst, time_limit_s=0.3)
        check = validate(inst, tours)
        assert check.feasible, (seed, check.errors)
        assert len(tours) == 2 and trace and trace[-1][1] >= check.score


def test_ortools_reaches_small_optima() -> None:
    from tests.test_routing_ils import brute_force_single_tour

    for seed in range(5):
        inst = random_instance(seed)
        tours, _ = ortools_solver.solve(inst, time_limit_s=0.3)
        assert validate(inst, tours).score == brute_force_single_tour(inst)
