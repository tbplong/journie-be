"""POST /routing against the real retrieval_test data (opening hours as stored). No OSRM: travel
is the straight-line estimate, so the test does not depend on a network service.

    uv run pytest -m integration
"""

import httpx
import pytest
from psycopg import AsyncConnection

from app.core.config import Settings
from app.schemas.routing import DaysResponse, RoutingRequest, RoutingResponse
from app.services.routing.rules import RULES
from app.services.routing.service import plan, plan_days
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
            return await plan(conn, client, settings, body)

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
            return await plan_days(conn, client, settings, body)

    result = run(job)
    placed = [p.id for day in result.days for p in day.places] + [
        item.poi.id for item in result.unscheduled
    ]
    assert sorted(placed) == sorted(cards)
    shares = [day.busy_minutes / day.capacity_minutes for day in result.days]
    assert max(shares) - min(shares) <= RULES.balance_tolerance + 0.01  # minutes are rounded
