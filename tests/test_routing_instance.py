from pathlib import Path

import pytest

from app.services.routing.instance import Instance, Node, read_benchmark
from app.services.routing.validate import validate

# KU Leuven format. Node 2 has a 2-value list, so its window is not at a fixed column.
# Times x100: node 1 serves 100, window [0, 5000]; node 2 serves 200, window [500, 1500];
# node 3 window [0, 10000]; tmax = depot close = 2000. Distances: 0-1 = 5, 0-2 = 1.55,
# 0-3 = 10, 1-3 = 5.
TINY = """4 2 3 1
0 200
0 0 0 0 0 0 0 0 20
1 3 4 1 10 1 1 1 0 50
2 0 1.55 2 20 1 2 1 2 5 15
3 6 8 0 5 1 1 1 0 100
"""


def tiny(tmp_path: Path, name: str = "c999") -> Instance:
    path = tmp_path / f"{name}.txt"
    path.write_text(TINY)
    return read_benchmark(path)


def test_reader_takes_the_window_from_the_last_columns(tmp_path: Path) -> None:
    instance = tiny(tmp_path)
    assert instance.tours == ((0, 2000), (0, 2000))
    assert [n.score for n in instance.nodes] == [0, 10, 20, 5]
    assert (instance.nodes[2].service, instance.nodes[2].windows) == (200, ((500, 1500),))
    assert list(instance.customers) == [1, 2, 3]


def test_distances_are_floored_per_instance_family(tmp_path: Path) -> None:
    assert tiny(tmp_path).travel[0][2] == 150  # Solomon: 1.55 -> 1.5
    assert tiny(tmp_path, "pr99").travel[0][2] == 155  # Cordeau: 2 decimals
    assert tiny(tmp_path).travel[0][1] == tiny(tmp_path).travel[1][0] == 500  # exact 5.0 kept


def test_reader_rejects_a_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "c998.txt"
    path.write_text(TINY.rsplit("\n", 2)[0])
    with pytest.raises(ValueError, match="node lines"):
        read_benchmark(path)


def test_m_can_be_overridden(tmp_path: Path) -> None:
    path = tmp_path / "c997.txt"
    path.write_text(TINY)
    assert read_benchmark(path, m=4).m == 4


def test_cordeau_default_tours_are_vehicles_times_depots(tmp_path: Path) -> None:
    assert tiny(tmp_path, "pr98").m == 2 * 1  # header "4 2 3 1": 2 vehicles, 1 depot
    path = tmp_path / "pr97.txt"
    path.write_text(TINY.replace("4 2 3 1", "5 3 3 4", 1))
    assert read_benchmark(path).m == 12
    path.write_text(TINY.replace("4 2 3 1", "4 3 3 4", 1))
    assert read_benchmark(path.rename(tmp_path / "c996.txt")).m == 3  # Solomon: v only


def test_feasible_solution_waits_for_opening_and_is_rescored(tmp_path: Path) -> None:
    check = validate(tiny(tmp_path), [[1], [2]])  # node 2: arrive 150, wait until 500
    assert check.feasible and check.score == 30
    assert validate(tiny(tmp_path), []).score == 0


@pytest.mark.parametrize(
    ("tours", "message"),
    [
        ([[3, 2]], "node 2 reached at 1880 after its last window"),
        ([[3, 1]], "back at depot at 2100 > end 2000"),
        ([[1, 1]], "more than once"),
        ([[1], [2], [3]], "3 tours > m = 2"),
        ([[0]], "not customer ids"),
    ],
)
def test_violations_are_reported(tmp_path: Path, tours: list[list[int]], message: str) -> None:
    check = validate(tiny(tmp_path), tours)
    assert not check.feasible
    assert any(message in error for error in check.errors), check.errors


def test_several_windows_wait_for_the_next_one_and_each_tour_has_its_own_day() -> None:
    # One node 10 away with windows [0, 50] and [200, 300]; day 0 is [0, 1000], day 1 [500, 600].
    depot = Node(0, 0, 0, 0, ((0, 1000),))
    node = Node(10, 0, 5, 7, ((0, 50), (200, 300)))
    travel = ((0, 10), (10, 0))
    late_start = Instance("w", (depot, node), travel, ((60, 1000), (500, 600)))
    assert node.start_at(60) == (200, 300) and node.start_at(301) is None
    assert validate(late_start, [[1], []]).feasible  # arrives 70, waits until 200
    check = validate(late_start, [[], [1]])  # day 1 begins at 500: every window has closed
    assert any("after its last window" in error for error in check.errors)
