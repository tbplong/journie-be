"""Day split (balance, travel rules, pins) and POST /routing/days. No database: the planner gets
fake POIs; travel is the straight-line estimate."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.retrieval import get_connection
from app.core.config import get_settings
from app.main import app
from app.schemas.routing import DaysResponse, RoutingRequest
from app.services.retrieval.db import reuse
from app.services.retrieval.poi import Poi
from app.services.routing import service
from app.services.routing.rules import RULES
from tests.test_routing_api import (  # noqa: F401 (client is a fixture)
    SETTINGS,
    card,
    client,
    fake_db,
    no_connection,
    plan,
    poi,
    request,
)

TWO_DAYS = ("2026-11-20T08:00", "2026-11-21T21:00")
TWO_DAYS_DATES = ["2026-11-20", "2026-11-21"]


def days_plan(monkeypatch: pytest.MonkeyPatch, pois: list[Poi], body: RoutingRequest):
    fake_db(monkeypatch, pois)

    async def run() -> DaysResponse:
        async with httpx.AsyncClient() as http:
            return await service.plan_days(reuse(None), http, SETTINGS, body)  # type: ignore[arg-type]

    return asyncio.run(run())


def landmarks(prefix: str, count: int, dlat: float = 0.0) -> list[Poi]:
    return [poi(f"{prefix}{i}", dlat + 0.002 * i, "attraction.landmark") for i in range(count)]


def ids(result: DaysResponse) -> list[list[str]]:
    return [[place.id for place in day.places] for day in result.days]


def test_days_are_balanced_within_the_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = landmarks("l", 6)
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance
    assert sum(map(len, ids(result))) == 6 and not result.unscheduled


def test_within_the_tolerance_each_day_keeps_to_one_area(monkeypatch: pytest.MonkeyPatch) -> None:
    near, far = landmarks("n", 3), landmarks("f", 3, dlat=0.05)  # ~5.5 km apart
    order = [p.id for pair in zip(near, far, strict=True) for p in pair]  # mixed on purpose
    result = days_plan(monkeypatch, near + far, request(order, *TWO_DAYS))
    assert sorted("".join(sorted({place[0] for place in day})) for day in ids(result)) == ["f", "n"]


def test_at_most_two_cafes_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    cafes = [poi(f"c{i}", 0.002 * i, "food.cafe") for i in range(4)]
    result = days_plan(monkeypatch, cafes, request([p.id for p in cafes], *TWO_DAYS))
    assert sorted(map(len, ids(result))) == [2, 2]
    assert not [w for w in result.warnings if w.startswith("day_max")]


def test_a_rule_that_cannot_hold_is_reported_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    cafes = [poi(f"c{i}", 0.002 * i, "food.cafe") for i in range(3)]
    body = request([p.id for p in cafes], "2026-11-20T08:00", "2026-11-20T21:00")
    result = days_plan(monkeypatch, cafes, body)
    assert sorted(ids(result)[0]) == ["c0", "c1", "c2"]  # all kept
    assert "day_max:food.cafe:2026-11-20" in result.warnings


def test_restaurants_are_spread_and_never_back_to_back(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [poi(f"r{i}", 0.002 * i) for i in range(4)] + landmarks("l", 4, dlat=0.001)
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    per_day = [[place.id[0] for place in day.places] for day in result.days]
    assert sorted(day.count("r") for day in per_day) == [2, 2]
    assert all("rr" not in "".join(day) for day in per_day)  # a landmark between restaurants
    assert not [w for w in result.warnings if not w.startswith("travel_estimated")]


def test_pins_override_the_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = landmarks("l", 4)
    pins = [{"id": p.id, "date": "2026-11-21"} for p in pois]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS, pins=pins))
    assert ids(result)[0] == [] and sorted(ids(result)[1]) == [p.id for p in pois]


def test_a_place_pinned_to_a_closed_day_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    friday_only = poi("fri", 0.003, hours={"fri": [["09:00", "17:00"]]})
    body = request(["fri"], *TWO_DAYS, pins=[{"id": "fri", "date": "2026-11-21"}])
    result = days_plan(monkeypatch, [friday_only], body)
    assert [(u.poi.id, u.reason) for u in result.unscheduled] == [("fri", "closed_on_pinned_day")]
    assert [str(d) for d in result.open_dates["fri"]] == ["2026-11-20"]


def test_routing_with_every_place_pinned_keeps_the_split(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = landmarks("l", 4)
    split = {"l0": "2026-11-20", "l1": "2026-11-21", "l2": "2026-11-21", "l3": "2026-11-21"}
    pins = [{"id": key, "date": day} for key, day in split.items()]
    result = plan(monkeypatch, pois, request(list(split), *TWO_DAYS, pins=pins))
    assert [sorted(s.poi.id for s in day.stops) for day in result.days] == [
        ["l0"],
        ["l1", "l2", "l3"],
    ]


@pytest.mark.parametrize(
    ("pins", "message"),
    [
        ([{"id": "zzz", "date": "2026-11-20"}], "pins not in places"),
        ([{"id": "a", "date": "2026-11-25"}], "pins outside the trip dates"),
        ([{"id": "a", "date": "2026-11-20"}] * 2, "pins contains the same id twice"),
    ],
)
def test_bad_pins_are_rejected(client: TestClient, pins: list, message: str) -> None:  # noqa: F811
    app.dependency_overrides[get_connection] = no_connection
    body = {
        "places": [card("a")], "start": {"lat": 10.77, "lon": 106.7},
        "start_at": TWO_DAYS[0], "end_at": TWO_DAYS[1], "pins": pins,
    }  # fmt: skip
    response = client.post("/routing/days", json=body)
    assert response.status_code == 422 and message in response.text


def test_days_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    fake_db(monkeypatch, landmarks("l", 2))
    app.dependency_overrides[get_connection] = no_connection
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    body = {
        "places": [card("l0"), card("l1")], "start": {"lat": 10.7725, "lon": 106.698},
        "start_at": TWO_DAYS[0], "end_at": TWO_DAYS[1],
    }  # fmt: skip
    response = client.post("/routing/days", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [day["date"] for day in data["days"]] == ["2026-11-20", "2026-11-21"]
    assert all(day["capacity_minutes"] == 13 * 60 for day in data["days"])
    assert data["open_dates"] == {"l0": TWO_DAYS_DATES, "l1": TWO_DAYS_DATES}
    assert data["meta"]["rules_version"] == RULES.version


def test_food_heavy_places_are_still_balanced(monkeypatch: pytest.MonkeyPatch) -> None:
    # T1: with one food place on the light day, every food move used to clash and was refused.
    pois = [
        *(poi(f"c{i}", 0.002 * i, "food.cafe") for i in range(4)),
        *(poi(f"r{i}", -0.002 * i) for i in range(2)),
        *landmarks("l", 4, dlat=0.001),
    ]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance, ids(result)
    assert not [w for w in result.warnings if not w.startswith("travel_estimated")]


def test_spare_days_are_used_on_a_long_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Moving a place to one of several empty days leaves the spread as it is; it must still pay.
    pois = landmarks("l", 16)
    body = request([p.id for p in pois], "2026-11-20T08:00", "2026-11-27T21:00")  # 8 days
    result = days_plan(monkeypatch, pois, body)
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance, ids(result)


def test_restaurants_are_split_by_another_stop_when_hours_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T4: restaurants open at 11:00 only; a cinema (150 min) and a pagoda can go between them.
    open_11 = {day: [["11:00", "22:00"]] for day in ("fri", "sat")}
    pois = [
        *(poi(f"r{i}", 0.003 * i, hours=open_11) for i in range(3)),
        poi("cinema", 0.004, "entertainment.cinema", hours={"fri": [["09:00", "23:00"]]}),
        poi("pagoda", -0.003, "culture_religion.pagoda", hours=None),
    ]
    result = plan(
        monkeypatch, pois, request([p.id for p in pois], "2026-11-20T08:00", "2026-11-20T21:00")
    )
    [day] = result.days
    order = "".join(stop.poi.id[0] for stop in day.stops)
    assert len(order) == 5 and "rr" not in order, order
    assert not [w for w in result.warnings if w.startswith("not_adjacent")]


def test_the_same_request_gives_the_same_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [poi(f"r{i}", 0.002 * i) for i in range(6)] + landmarks("l", 10, dlat=0.001)
    body = request([p.id for p in pois], "2026-11-20T08:00", "2026-11-23T21:00")
    assert ids(days_plan(monkeypatch, pois, body)) == ids(days_plan(monkeypatch, pois, body))
