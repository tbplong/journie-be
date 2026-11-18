from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from psycopg import AsyncConnection

from app.api.routes.retrieval import get_connection
from app.core.config import Settings, get_settings
from app.core.errors import problem_doc
from app.schemas.routing import DaysResponse, RoutingRequest, RoutingResponse
from app.services.routing.service import plan, plan_days

router = APIRouter(prefix="/routing", tags=["routing"])


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Order the picked places into a day-by-day itinerary",
    response_description="Stops per day in visiting order, with times and travel legs",
    responses={
        500: problem_doc("`routing-invalid-plan`", "/routing"),
        503: problem_doc("`retrieval-not-configured` or `retrieval-db-unavailable`", "/routing"),
    },
)
async def routing(
    body: RoutingRequest,
    request: Request,
    conn: Annotated[AsyncConnection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RoutingResponse:
    """Plan one tour per trip day from `start` and back, visiting as many of `places` as fit
    (`must_visit_ids` first) inside opening hours and the day's time. Places in `pins` stay on
    their day; the others are spread so days are balanced (within 15 points of their length)
    and the travel rules hold (config/rules.yaml; broken ones are listed in `warnings`).

    Read `days[].stops` top to bottom. Places that do not fit are listed in `unscheduled` with a
    reason. Travel times come from OSRM road distances (walk below 1 km at 4.5 km/h, ride at
    20 km/h); without OSRM they are straight-line estimates and `warnings` has
    `travel_estimated`. Visit durations are per category (placeholder values).
    """
    return await plan(conn, request.app.state.http_client, settings, body)


DAYS_RESPONSES = {
    500: problem_doc("`routing-invalid-plan`", "/routing/days"),
    503: problem_doc("`retrieval-not-configured` or `retrieval-db-unavailable`", "/routing/days"),
}


@router.post(
    "/days",
    status_code=status.HTTP_200_OK,
    summary="Suggest which places to visit on which day",
    response_description="Places per day, each day's load, and the days each place is open",
    responses=DAYS_RESPONSES,
)
async def routing_days(
    body: RoutingRequest,
    request: Request,
    conn: Annotated[AsyncConnection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DaysResponse:
    """Same request and planner as POST /routing, answered as one list of places per day for
    the traveller to review. Moving a place: keep it on a date of its `open_dates`. To ask
    again after edits, send the edited places as `pins`; once every place is pinned, POST
    /routing returns the timed itinerary for that split.
    """
    return await plan_days(conn, request.app.state.http_client, settings, body)
