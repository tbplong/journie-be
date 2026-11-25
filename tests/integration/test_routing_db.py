"""POST /routing against the real retrieval_test data (opening hours as stored). No OSRM: travel
is the straight-line estimate, so the test does not depend on a network service.

    uv run pytest -m integration
"""

from datetime import datetime, time, timedelta

import httpx
import pytest
from psycopg import AsyncConnection

from app.core.config import Settings
from app.schemas.routing import TRIP_TZ, DaysResponse, RoutingRequest, RoutingResponse
from app.services.retrieval.db import reuse
from app.services.routing.rules import RULES
from app.services.routing.service import plan, plan_days
from app.services.routing.visits import VISIT_MINUTES
from tests.integration.test_retrieval_db import retrieval, run

pytestmark = pytest.mark.integration


def test_retrieved_places_become_a_feasible_two_day_plan() -> None:
    found = retrieval(interests=["cafe", "culture"], must_visit=["Dinh Độc Lập"])
    cards = {card.id: card for card in found.pinned}
    for section in found.sections:
        cards.update((card.id, card) for card in section.items[:6])
    body = RoutingRequest.model_validate(
        {
            "places": [card.model_dump() for card in cards.values()],
            "must_visit_ids": [card.id for card in found.pinned],
            "start": {"lat": 10.7725, "lon": 106.6980},
            "start_at": "2026-11-21T08:00",
            "end_at": "2026-11-22T21:00",
        }
    )

    async def job(conn: AsyncConnection) -> RoutingResponse:
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, routing_time_limit_seconds=3.0)
            return await plan(reuse(conn), client, settings, body)

    result = run(job)
    stops = [stop for day in result.days for stop in day.stops]
    placed = [stop.poi.id for stop in stops] + [item.poi.id for item in result.unscheduled]
    assert sorted(placed) == sorted(cards)  # every place exactly once
    assert {card.id for card in found.pinned} <= {stop.poi.id for stop in stops}
    assert "not_found" not in {item.reason for item in result.unscheduled}
    assert len(result.days) == 2 and stops
    for day in result.days:
        previous = None
        for stop in day.stops:
            assert stop.arrive <= stop.start < stop.leave and stop.start.date() == day.date
            assert previous is None or previous.leave <= stop.arrive
            previous = stop


def test_days_from_real_data_are_balanced_and_keep_every_place() -> None:
    found = retrieval(interests=["cafe", "culture"])
    cards = {card.id: card for section in found.sections for card in section.items[:6]}
    body = RoutingRequest.model_validate(
        {
            "places": [card.model_dump() for card in cards.values()],
            "start": {"lat": 10.7725, "lon": 106.6980},
            "start_at": "2026-11-21T08:00",
            "end_at": "2026-11-22T21:00",
        }
    )

    async def job(conn: AsyncConnection) -> DaysResponse:
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, routing_time_limit_seconds=3.0)
            return await plan_days(reuse(conn), client, settings, body)

    result = run(job)
    placed = [p.id for day in result.days for p in day.places] + [
        item.poi.id for item in result.unscheduled
    ]
    assert sorted(placed) == sorted(cards)
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance + 0.01  # minutes are rounded


def solve_ids(ids: list[str], end_at: str, days: bool, **extra: object):
    """/routing/days (days=True) or /routing for these place ids, from 2026-11-21 08:00."""
    from tests.test_routing_api import card

    body = RoutingRequest.model_validate(
        {
            "places": [card(i) for i in ids],
            "start": {"lat": 10.7725, "lon": 106.6980},
            "start_at": "2026-11-21T08:00",
            "end_at": end_at,
            **extra,
        }
    )

    async def job(conn: AsyncConnection) -> DaysResponse | RoutingResponse:
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, routing_time_limit_seconds=6.0)
            return await (plan_days if days else plan)(reuse(conn), client, settings, body)

    return run(job)


def test_cafe_heavy_places_are_split_evenly() -> None:
    ids = [
        "ca-phe-coi-xua-b4e977", "cafe-thuc-2c0f2d", "96b-cafe-roastery-4ed744",
        "saigon-chic-coffee-ea47db", "nha-hang-chay-an-duyen-52e095", "morico-ef8c5a",
        "green-palm-gallery-40bcbf", "san-khau-kich-hoang-thai-thanh-368d13",
        "hai-an-gallery-369e91", "chua-an-phu-1e8bcd",
    ]  # fmt: skip
    result = solve_ids(ids, "2026-11-22T21:00", days=True)
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance + 0.01
    assert not [w for w in result.warnings if w.startswith(("day_max", "day_spread"))]


def test_restaurants_are_kept_apart_when_another_stop_fits_between() -> None:
    ids = [
        "parkhaus-thao-dien-df4d3d", "muteki-4a8987", "chang-modern-thai-cuisine-bbb6c4",
        "lotte-cinema-cong-hoa-b69fa6", "chua-quan-am-e3f7f5",
    ]  # fmt: skip
    result = solve_ids(ids, "2026-11-21T21:00", days=False)
    assert len(result.days[0].stops) == 5
    assert not [w for w in result.warnings if w.startswith("not_adjacent")]


def test_pinning_the_suggested_split_keeps_every_place() -> None:
    found = retrieval(interests=["cafe", "culture", "food", "nightlife", "shopping"])
    ids = list(dict.fromkeys(card.id for section in found.sections for card in section.items))[:40]
    days = solve_ids(ids, "2026-11-22T21:00", days=True)
    split = [[place.id for place in day.places] for day in days.days]
    pins = [
        {"id": i, "date": str(day.date)} for day, day_ids in zip(days.days, split) for i in day_ids
    ]
    ordered = [i for day_ids in split for i in day_ids]  # in /routing/days' visiting order
    routed = solve_ids(ordered, "2026-11-22T21:00", days=False, pins=pins)
    assert [[stop.poi.id for stop in day.stops] for day in routed.days] and not routed.unscheduled
    assert [len(day.stops) for day in routed.days] == list(map(len, split))


def test_days_that_end_at_three_keep_to_their_hours_and_rest() -> None:
    found = retrieval(interests=["nightlife", "food", "culture"])
    cards = {card.id: card for section in found.sections for card in section.items[:5]}
    body = RoutingRequest.model_validate(
        {
            "places": [card.model_dump() for card in cards.values()],
            "start": {"lat": 10.7725, "lon": 106.6980},
            "start_at": "2026-11-21T09:00",  # Saturday and Sunday nights, into Monday
            "end_at": "2026-11-23T03:00",
            "day_start": "09:00",
            "day_end": "03:00",
        }
    )

    async def job(conn: AsyncConnection) -> tuple[RoutingResponse, DaysResponse]:
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, routing_time_limit_seconds=3.0)
            routed = await plan(reuse(conn), client, settings, body)
            return routed, await plan_days(reuse(conn), client, settings, body)

    result, days = run(job)
    assert [day.capacity_minutes for day in days.days] == [18 * 60, 18 * 60]
    placed = [stop.poi.id for day in result.days for stop in day.stops]
    assert sorted(placed + [item.poi.id for item in result.unscheduled]) == sorted(cards)
    assert len(result.days) == 2 and placed
    for day in result.days:
        opens = datetime.combine(day.date, time(9), TRIP_TZ)
        previous = None
        for stop in day.stops:  # 09:00 to 03:00 the next morning
            assert opens <= stop.arrive <= stop.start < stop.leave <= opens + timedelta(hours=18)
            assert previous is None or previous.leave <= stop.arrive
            previous = stop
    first, second = (day.stops for day in result.days)
    if first and second:
        assert (second[0].arrive - first[-1].leave) >= timedelta(hours=6)


def test_every_place_value_names_a_stored_place() -> None:
    async def job(conn: AsyncConnection) -> set[str]:
        cur = await conn.execute(
            "SELECT id FROM pois WHERE id = ANY(%s)", [list(VISIT_MINUTES.by_place)]
        )
        return {row[0] for row in await cur.fetchall()}

    assert run(job) == set(VISIT_MINUTES.by_place)
