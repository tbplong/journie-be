"""TOPTW instance model and the KU Leuven benchmark reader (https://www.mech.kuleuven.be/en/mim/op).

Team Orienteering Problem with Time Windows: every tour starts and ends at node 0 inside its own
[begin, end] (one tour per trip day); each other node may be visited at most once, service must
*start* inside one of its windows (arriving early means waiting), and the goal is the largest
total score.

Multi-day trips use one continuous timeline (day d is [d * day length, (d + 1) * day length)),
so a node's windows are the union of its per-day opening hours and the tour of day d can only
reach day d's windows. The benchmark instances have one window per node and identical tours.

Times are integers in TIME_UNIT steps (1/100) so that the benchmark rounding is exact and
OR-Tools, which needs integers, sees the same numbers as every other solver.
"""

import math
from dataclasses import dataclass
from pathlib import Path

TIME_UNIT = 100  # time values are stored as round(value * TIME_UNIT)
# Benchmark rule: Euclidean distance rounded *down* to 1 decimal (Solomon-based instances) or
# 2 decimals (Cordeau-based "pr" instances). The epsilon keeps exact values such as 5.0 from
# flooring to 4.9 through float error.
FLOOR_EPSILON = 1e-9

Window = tuple[int, int]  # (earliest, latest) start of service, or a tour's (begin, end)


@dataclass(frozen=True)
class Node:
    x: float
    y: float
    service: int
    score: int
    windows: tuple[Window, ...]  # sorted and disjoint

    def start_at(self, arrive: int) -> Window | None:
        """(start of service, close of that window) for an arrival time, or None if every
        window has closed. The earliest window is always the best choice."""
        for open_, close in self.windows:
            if arrive <= close:
                return max(arrive, open_), close
        return None


@dataclass(frozen=True)
class Instance:
    name: str
    nodes: tuple[Node, ...]  # node 0 is the start and end of every tour
    travel: tuple[tuple[int, ...], ...]  # travel[i][j], same unit as the node times
    tours: tuple[Window, ...]  # per tour (day): leave node 0 at begin, be back by end

    @property
    def m(self) -> int:
        return len(self.tours)

    @property
    def customers(self) -> range:
        return range(1, len(self.nodes))


def _units(value: str) -> int:
    return round(float(value) * TIME_UNIT)


def _floored_distance(a: Node, b: Node, decimals: int) -> int:
    step = 10**decimals
    distance = math.floor(math.hypot(a.x - b.x, a.y - b.y) * step + FLOOR_EPSILON)
    return distance * (TIME_UNIT // step)


def read_benchmark(path: Path, m: int | None = None) -> Instance:
    """Read a KU Leuven TOPTW file. ``m`` defaults to the file's own tour count (the "OPT"
    setting, where visiting every customer is feasible and therefore optimal).

    Line 1: ``k v N t`` (v = tours that can visit every node, N = customers). Line 2 is unused.
    Cordeau "pr" files keep their multi-depot header (v vehicles per depot, t depots): there the
    tour count is v * t. With v alone pr01 cannot visit all 48 customers (best 611 of 657);
    with v * t = 12 both solvers visit all of them.
    Then one line per node: ``i x y d S f a <a values> O C``; the list length varies, so the
    window is read from the last two columns. Every tour runs from node 0's open to its close.
    """
    lines = [line.split() for line in path.read_text().splitlines() if line.strip()]
    header, rows = lines[0], lines[2:]
    customers = int(header[2])
    if len(rows) != customers + 1:
        raise ValueError(f"{path.name}: expected {customers + 1} node lines, got {len(rows)}")
    nodes = tuple(
        Node(
            float(r[1]),
            float(r[2]),
            _units(r[3]),
            round(float(r[4])),
            ((_units(r[-2]), _units(r[-1])),),
        )
        for r in rows
    )
    if [int(r[0]) for r in rows] != list(range(len(rows))):
        raise ValueError(f"{path.name}: node ids are not 0..{customers}")
    cordeau = path.stem.startswith("pr")
    travel = tuple(
        tuple(_floored_distance(a, b, 2 if cordeau else 1) for b in nodes) for a in nodes
    )
    tours = m or int(header[1]) * (int(header[3]) if cordeau else 1)
    return Instance(path.stem, nodes, travel, (nodes[0].windows[0],) * tours)
