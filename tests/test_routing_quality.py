"""What makes a valid plan a good one: little waiting, meals at meal times, no needless detour,
no place left out when another order of the day holds it. And names are never swapped."""

import itertools
import random
import time

import pytest

from app.services.retrieval.names import NameHit, classify, one_letter_apart
from app.services.routing import ils
from app.services.routing.instance import Instance, Node
from app.services.routing.rules import RULES
from app.services.routing.validate import tour_errors
from tests.test_routing_api import plan, poi, request

DAY = (8 * 3600, 21 * 3600)
ONE_DAY = ("2026-11-20T08:00", "2026-11-20T21:00")


def instance(nodes: list[Node]) -> Instance:
    points = [(n.x, n.y) for n in nodes]
    travel = tuple(
        tuple(0 if a == b else round(abs(a[0] - b[0]) + abs(a[1] - b[1])) for b in points)
        for a in points
    )
    return Instance("t", tuple(nodes), travel, (DAY,))


def finish(inst: Instance, order: tuple[int, ...]) -> int | None:
    moment, here = inst.tours[0][0], 0
    for j in order:
        slot = inst.nodes[j].start_at(moment + inst.travel[here][j])
        if slot is None:
            return None
        moment, here = slot[0] + inst.nodes[j].service, j
    back = moment + inst.travel[here][0]
    return back if back <= inst.tours[0][1] else None


@pytest.mark.parametrize(("similarity", "status"), [(0.667, "ambiguous"), (0.688, "resolved")])
def test_a_name_sharing_its_first_words_is_not_swapped_in(similarity: float, status: str) -> None:
    assert classify("q", [NameHit("a", similarity, False)]).status == status


def test_a_one_letter_typo_still_resolves() -> None:
    assert classify("cho ben yhanh", [NameHit("a", 0.647, False, typo=True)]).status == "resolved"
    assert classify("q", [NameHit("a", 0.59, False, typo=True)]).status == "ambiguous"
    assert one_letter_apart("cho ben yhanh", "cho ben thanh")
    assert one_letter_apart("cho tan inh", "cho tan dinh") and one_letter_apart("ab", "ab")
    assert not one_letter_apart("chua thien mu", "chua thien minh")


@pytest.mark.parametrize("seed", range(25))
def test_best_order_is_the_least_lost_time(seed: int) -> None:
    rng = random.Random(seed)
    nodes = [Node(0, 0, 0, 0, (DAY,))]
    for _ in range(6):
        opens = rng.randrange(8 * 3600, 17 * 3600)
        nodes.append(
            Node(rng.randrange(4000), rng.randrange(4000), 1800, 1, ((opens, opens + 14400),))
        )
    inst = instance(nodes)
    ids = list(inst.customers)

    def lost(order: tuple[int, ...]) -> int | None:
        if finish(inst, order) is None:
            return None
        total, moment, here = 0, inst.tours[0][0], 0
        for k, j in enumerate(order):
            arrive = moment + inst.travel[here][j]
            start = inst.nodes[j].start_at(arrive)[0]
            total += inst.travel[here][j] + (start - arrive if k else 0)
            moment, here = start + inst.nodes[j].service, j
        return total + inst.travel[here][0]

    values = [v for p in itertools.permutations(ids) if (v := lost(p)) is not None]

    def wait(_j: int, arrive: int, start: int, first: bool, state: int) -> tuple[int, int]:
        return (0 if first else start - arrive), state

    order = ils.best_order(inst, 0, ids, inst.clash, wait)
    assert (order is not None) == bool(values)
    if order:
        assert lost(tuple(order)) == min(values)


def test_a_pinned_place_outweighs_free_places(monkeypatch) -> None:
    # The day holds one 2 h visit: the pinned one stays even though it is the last in the request.
    pois = [poi(f"f{k}", 0.001 * k, "attraction.museum", hours=None) for k in range(1, 4)]
    pois.append(poi("pin", 0.02, "attraction.museum", hours=None))
    pins = [{"id": "pin", "date": "2026-11-20"}]
    body = request([p.id for p in pois], "2026-11-20T08:00", "2026-11-20T10:30", pins=pins)
    result = plan(monkeypatch, pois, body)
    assert [s.poi.id for s in result.days[0].stops] == ["pin"]


@pytest.mark.parametrize("seed", range(40))
def test_exact_order_finds_an_order_whenever_one_exists(seed: int) -> None:
    rng = random.Random(seed)
    nodes = [Node(0, 0, 0, 0, (DAY,))]
    for _ in range(6):
        opens = rng.randrange(8 * 3600, 17 * 3600)
        nodes.append(
            Node(rng.randrange(4000), rng.randrange(4000), 3600, 1, ((opens, opens + 7200),))
        )
    inst = instance(nodes)
    ids = list(inst.customers)
    finishes = [f for p in itertools.permutations(ids) if (f := finish(inst, p)) is not None]
    order = ils.exact_order(inst, 0, ids)
    assert (order is not None) == bool(finishes)
    if order:
        assert sorted(order) == ids and not tour_errors(inst, order, 0)
        assert finish(inst, tuple(order)) == min(finishes)


def test_fill_adds_a_place_that_only_fits_after_reordering() -> None:
    # Node 1 is planned first; node 2 closes before node 1 can end, so it must go first.
    nodes = [
        Node(0, 0, 0, 0, (DAY,)),
        Node(10, 0, 3600, 1, ((8 * 3600, 12 * 3600),)),
        Node(20, 0, 3600, 1, ((8 * 3600, 8 * 3600 + 600),)),
    ]
    inst = instance(nodes)
    filled = ils.fill(inst, [[1]], lambda tours: (0, sum(map(len, tours))), time.perf_counter() + 5)
    assert filled == [[2, 1]]


def test_meal_hours_rule() -> None:
    assert RULES.is_meal("food.restaurant") and not RULES.is_meal("food.cafe")
    assert not RULES.off_meal_hours(12 * 3600) and RULES.off_meal_hours(15 * 3600)
    assert RULES.meal_penalty == 20 * 60


def test_a_plan_does_not_wait_when_another_order_does_not(monkeypatch) -> None:
    late = {day: [["15:00", "21:00"]] for day in ("fri",)}
    pois = [poi("late", 0.001, "attraction.museum", hours=late), poi("a", 0.002, "attraction.museum")]
    pois.append(poi("b", 0.003, "attraction.museum"))
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY))
    [day] = result.days
    assert sum(stop.wait_minutes for stop in day.stops[1:]) == 0


def test_lunch_lands_at_lunch_time(monkeypatch) -> None:
    pois = [poi(f"m{k}", 0.001 * k, "attraction.museum") for k in range(1, 4)]
    pois.append(poi("lunch", 0.002, "food.restaurant"))
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY))
    start = next(s.start for s in result.days[0].stops if s.poi.id == "lunch")
    assert not RULES.off_meal_hours(start.hour * 3600 + start.minute * 60)
