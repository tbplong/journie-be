"""TOPTW with the OR-Tools routing solver (prize-collecting VRP with time windows).

Each customer is optional (``AddDisjunction``) with a drop penalty of score * scale, where
scale exceeds any total travel time, so the objective (travel + penalties) is minimised
by the largest score first and the shortest travel second. Arc costs are kept (not zero) so that
Guided Local Search has something to guide on.

Several windows per node: the cumul range is [first open, last close] with the gaps removed
(``RemoveInterval``). Tour d = vehicle d, whose start and end cumuls are bounded by its day.
"""

import time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.services.routing.instance import Instance
from app.services.routing.validate import Tours

PENALTY_SCALE = 10**7  # minimum drop penalty per score point; raised when tours are longer

Trace = list[tuple[float, int]]  # (seconds since start, score) for each improving solution


def solve(instance: Instance, time_limit_s: float, seed: int = 0) -> tuple[Tours, Trace]:
    """``seed`` is unused: OR-Tools routing with GLS is deterministic for a given model."""
    del seed
    n = len(instance.nodes)
    horizon = max(end for _, end in instance.tours)
    # Total travel <= total tour time, so one score point outweighs any travel saving.
    scale = max(PENALTY_SCALE, sum(end - begin for begin, end in instance.tours) + 1)
    manager = pywrapcp.RoutingIndexManager(n, instance.m, 0)
    routing = pywrapcp.RoutingModel(manager)

    def travel(from_index: int, to_index: int) -> int:
        i, j = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        return instance.travel[i][j]

    def duration(from_index: int, to_index: int) -> int:
        i = manager.IndexToNode(from_index)
        return instance.nodes[i].service + travel(from_index, to_index)

    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(travel))
    # Cumul at a node = start of service there; slack = waiting for the window to open.
    routing.AddDimension(routing.RegisterTransitCallback(duration), horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for node_id in instance.customers:
        node = instance.nodes[node_id]
        index = manager.NodeToIndex(node_id)
        cumul = time_dim.CumulVar(index)
        cumul.SetRange(node.windows[0][0], node.windows[-1][1])
        for (_, close), (next_open, _) in zip(node.windows, node.windows[1:]):
            cumul.RemoveInterval(close + 1, next_open - 1)
        routing.AddDisjunction([index], node.score * scale)
    for vehicle, (begin, end) in enumerate(instance.tours):
        time_dim.CumulVar(routing.Start(vehicle)).SetRange(begin, end)
        time_dim.CumulVar(routing.End(vehicle)).SetRange(begin, end)

    total = sum(instance.nodes[i].score for i in instance.customers)
    trace: Trace = []
    started = time.perf_counter()

    def on_solution() -> None:
        dropped = routing.CostVar().Value() // scale
        trace.append((time.perf_counter() - started, total - dropped))

    routing.AddAtSolutionCallback(on_solution)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromMilliseconds(round(time_limit_s * 1000))
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return [[] for _ in instance.tours], trace
    tours = []
    for vehicle in range(instance.m):
        index, tour = solution.Value(routing.NextVar(routing.Start(vehicle))), []
        while not routing.IsEnd(index):
            tour.append(manager.IndexToNode(index))
            index = solution.Value(routing.NextVar(index))
        tours.append(tour)
    return tours, trace
