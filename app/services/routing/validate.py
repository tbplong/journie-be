"""Independent feasibility check and score of a TOPTW solution. Solvers' own scores are never
trusted: every benchmark result is re-scored here."""

from dataclasses import dataclass

from app.services.routing.instance import Instance

Tours = list[list[int]]  # tours[d] = customer node ids of tour (day) d, depot excluded


@dataclass(frozen=True)
class Check:
    score: int
    errors: tuple[str, ...]

    @property
    def feasible(self) -> bool:
        return not self.errors


def tour_errors(instance: Instance, tour: list[int], day: int = 0) -> list[str]:
    """Window and end-time violations of one tour, simulated from the day's begin at node 0."""
    errors = []
    time, end = instance.tours[day]
    here = 0
    for node_id in tour:
        node = instance.nodes[node_id]
        arrive = time + instance.travel[here][node_id]
        slot = node.start_at(arrive)
        if slot is None:
            errors.append(f"node {node_id} reached at {arrive} after its last window")
            start = arrive
        else:
            start = slot[0]
        time, here = start + node.service, node_id
    back = time + instance.travel[here][0]
    if back > end:
        errors.append(f"back at depot at {back} > end {end}")
    return errors


def validate(instance: Instance, tours: Tours) -> Check:
    errors = []
    if len(tours) > instance.m:
        errors.append(f"{len(tours)} tours > m = {instance.m}")
    visited = [node_id for tour in tours for node_id in tour]
    if bad := [n for n in visited if n not in instance.customers]:
        errors.append(f"not customer ids: {sorted(set(bad))}")
    if len(visited) != len(set(visited)):
        errors.append("a customer is visited more than once")
    if not errors:
        for day, tour in enumerate(tours):
            errors += [f"tour {day}: {error}" for error in tour_errors(instance, tour, day)]
    score = sum(instance.nodes[n].score for n in set(visited) if n in instance.customers)
    return Check(score, tuple(errors))
