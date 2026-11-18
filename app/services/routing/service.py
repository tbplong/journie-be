"""POST /routing and /routing/days: spread the traveller's places over the trip days and order
each day (ILS on a TOPTW, then a balancing local search).

Every day starts and ends at the traveller's location. Times are integer seconds on one trip
timeline that starts at midnight of the first trip day (Asia/Ho_Chi_Minh). A place's windows are
its opening hours on each trip day, clipped to that day and shortened by the visit, so the visit
ends before closing; the tour of day d can only use day d's windows, and a pinned place keeps
only the windows of its day.

Plans are compared by, in order: places visited (must-visit first), broken travel rules
(config/rules.yaml), day balance beyond the tolerance, total travel.
"""

import asyncio
import math
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import yaml
from fastapi import status
from psycopg import AsyncConnection

from app.core.config import Settings
from app.core.errors import ProblemError
from app.schemas.retrieval import PoiCard
from app.schemas.routing import (
    TRIP_TZ,
    Day,
    DayPlan,
    DaysResponse,
    Leg,
    RoutingMeta,
    RoutingRequest,
    RoutingResponse,
    Stop,
    Unscheduled,
)
from app.services.retrieval.candidates import fetch_by_ids
from app.services.retrieval.hours import DAY_MIN, WeeklyHours, to_minutes, weekly_intervals
from app.services.retrieval.poi import Poi
from app.services.retrieval.scoring import haversine_km
from app.services.routing import ils
from app.services.routing.instance import Instance, Node, Window
from app.services.routing.osrm import road_km
from app.services.routing.rules import RULES
from app.services.routing.validate import Tours, validate

ROUTING_VERSION = "routing-v2"  # v2: day balance, travel rules, pins
DAY_S = 24 * 3600
WALK_THRESHOLD_KM = 1.0  # agreed: walk below 1 km of road distance, ride otherwise
WALK_KMH, RIDE_KMH = 4.5, 20.0  # agreed speeds; 20 km/h is a city-traffic motorbike average
DETOUR_FACTOR = 1.3  # road / straight-line distance, used when OSRM gives no distance
STALL_S = 1.0  # stop searching once the best plan has not improved for this long
VISIT_MINUTES_FILE = Path(__file__).with_name("config") / "visit_minutes.yaml"


@dataclass(frozen=True)
class VisitMinutes:
    version: str
    default: int
    by_category: dict[str, int]

    def of(self, category: str | None) -> int:
        return self.by_category.get(category or "", self.default)


def load_visit_minutes(path: Path = VISIT_MINUTES_FILE) -> VisitMinutes:
    """Loaded at import, so a bad edit fails at startup, not mid-request."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    table = VisitMinutes(data["version"], int(data["default"]), dict(data["categories"]))
    if any(not isinstance(v, int) or v <= 0 for v in [table.default, *table.by_category.values()]):
        raise ValueError(f"{path.name}: visit minutes must be positive integers")
    return table


VISIT_MINUTES = load_visit_minutes()


def day_bounds(request: RoutingRequest) -> list[Window]:
    """(begin, end) of each trip day on the trip timeline."""
    first, last = request.start_at, request.end_at
    days = (last.date() - first.date()).days + 1
    day_start, day_end = to_minutes(request.day_start) * 60, to_minutes(request.day_end) * 60
    bounds = []
    for d in range(days):
        begin = d * DAY_S + (_seconds(first) if d == 0 else day_start)
        end = d * DAY_S + day_end
        if d == days - 1:
            end = min(end, d * DAY_S + _seconds(last))
        bounds.append((begin, max(begin, end)))  # a day with no time left gets no visit
    return bounds


def _seconds(moment: datetime) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def windows(
    hours: WeeklyHours | None, service: int, bounds: list[Window], first_weekday: int
) -> tuple[Window, ...]:
    """Start-of-visit windows on the trip timeline; no hours on record = open all day."""
    intervals = weekly_intervals(hours)
    found = []
    for d, (begin, end) in enumerate(bounds):
        weekday = (first_weekday + d) % 7
        shift = d * DAY_S - weekday * DAY_MIN * 60  # week seconds -> trip seconds of day d
        opened = (
            [(begin, end)]
            if intervals is None
            else [(start * 60 + shift, stop * 60 + shift) for start, stop in intervals]
        )
        for start, stop in opened:
            first, last = max(start, begin), min(stop, end) - service
            if last >= first:
                found.append((first, last))
    merged: list[list[int]] = []
    for start, stop in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    return tuple((start, stop) for start, stop in merged)


def leg_seconds(km: float) -> int:
    return round(km / (WALK_KMH if km < WALK_THRESHOLD_KM else RIDE_KMH) * 3600)


def leg(km: float) -> Leg:
    mode = "walk" if km < WALK_THRESHOLD_KM else "ride"
    return Leg(mode=mode, distance_km=round(km, 2), minutes=round(leg_seconds(km) / 60))


async def distances(
    client: httpx.AsyncClient, settings: Settings, points: list[tuple[float, float]]
) -> tuple[list[list[float]], bool]:
    """km matrix and whether any of it is a straight-line estimate."""
    road = None
    if settings.osrm_url and len(points) > 1:
        road = await road_km(client, settings.osrm_url, points, settings.osrm_timeout_seconds)
    estimated = False
    km = []
    for i, a in enumerate(points):
        row = []
        for j, b in enumerate(points):
            value = 0.0 if i == j else (road[i][j] if road else None)
            if value is None:
                value, estimated = haversine_km(*a, *b) * DETOUR_FACTOR, True
            row.append(value)
        km.append(row)
    return km, estimated


@dataclass(frozen=True)
class Prepared:
    request: RoutingRequest
    instance: Instance  # node i (i >= 1) is located[i - 1]
    located: list[tuple[PoiCard, Poi]]
    unscheduled: list[Unscheduled]  # places that never reach the solver
    open_dates: dict[str, list[date]]
    km: list[list[float]]
    estimated: bool  # some travel is a straight-line estimate
    started: float

    @property
    def dates(self) -> list[date]:
        first = self.request.start_at.date()
        return [first + timedelta(days=d) for d in range(self.instance.m)]

    def day_categories(self, tours: Tours) -> tuple[list[list[str | None]], list[date]]:
        """Categories visited on each day that has time, with those days' dates."""
        active = [d for d, (begin, end) in enumerate(self.instance.tours) if end > begin]
        categories = [None] + [poi.category for _, poi in self.located]
        days = [[categories[j] for j in tours[d]] for d in active]
        return days, [self.dates[d] for d in active]


async def prepare(
    conn: AsyncConnection,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> Prepared:
    started = clock.perf_counter()
    found = await fetch_by_ids(conn, [place.id for place in request.places])
    bounds = day_bounds(request)
    first_day = request.start_at.date()
    pinned = {pin.id: (pin.date - first_day).days for pin in request.pins}
    must = set(request.must_visit_ids)
    unscheduled: list[Unscheduled] = []
    open_dates: dict[str, list[date]] = {}
    located: list[tuple[PoiCard, Poi, int, tuple[Window, ...]]] = []
    for card in request.places:
        poi = found.get(card.id)
        if poi is None or poi.latitude is None or poi.longitude is None:
            reason = "not_found" if poi is None else "no_location"
            unscheduled.append(Unscheduled(poi=card, reason=reason))
            continue
        service = VISIT_MINUTES.of(poi.category) * 60
        slots = windows(poi.opening_hours, service, bounds, request.start_at.weekday())
        open_days = sorted({start // DAY_S for start, _ in slots})  # a window stays in its day
        open_dates[card.id] = [first_day + timedelta(days=d) for d in open_days]
        if card.id in pinned:
            slots = tuple(w for w in slots if w[0] // DAY_S == pinned[card.id])
        if not slots:
            reason = "closed_on_pinned_day" if card.id in pinned else "closed_on_trip_days"
            unscheduled.append(Unscheduled(poi=card, reason=reason))
            continue
        located.append((card, poi, service, slots))

    must_score = len(located) + 1  # one must-visit place outweighs every other place together
    nodes = [Node(request.start.lat, request.start.lon, 0, 0, ((bounds[0][0], bounds[-1][1]),))]
    nodes += [
        Node(
            poi.latitude,
            poi.longitude,
            service,
            must_score if card.id in must else 1,
            slots,
            RULES.tag(poi.category),
        )
        for card, poi, service, slots in located
    ]
    km, estimated = await distances(client, settings, [(n.x, n.y) for n in nodes])
    travel = tuple(tuple(leg_seconds(value) for value in row) for row in km)
    instance = Instance(
        "request", tuple(nodes), travel, tuple(bounds), frozenset(RULES.not_adjacent)
    )
    return Prepared(
        request,
        instance,
        [(card, poi) for card, poi, *_ in located],
        unscheduled,
        open_dates,
        km,
        estimated,
        started,
    )


def busy_seconds(inst: Instance, tour: list[int]) -> tuple[int, int]:
    """(travel, travel + visits) of one day, from and back to the start."""
    moving = sum(inst.travel[a][b] for a, b in pairwise([0, *tour, 0])) if tour else 0
    return moving, moving + sum(inst.nodes[j].service for j in tour)


def plan_cost(prep: Prepared) -> Callable[[Tours], tuple[int, int, int]]:
    """(broken rules, balance points beyond the tolerance, travel seconds): lower is better."""
    inst = prep.instance
    capacity = [end - begin for begin, end in inst.tours]

    def cost(tours: Tours) -> tuple[int, int, int]:
        broken, _ = RULES.broken(*prep.day_categories(tours))
        loads, total_travel = [], 0
        for d, tour in enumerate(tours):
            moving, busy = busy_seconds(inst, tour)
            total_travel += moving
            if capacity[d] > 0:
                loads.append(busy / capacity[d])
        spread = max(loads) - min(loads) if loads else 0.0
        beyond = max(0, math.ceil(round((spread - RULES.balance_tolerance) * 100, 6)))
        return broken + inst.clashes(tours), beyond, total_travel

    return cost


def solve(prep: Prepared, time_limit_s: float) -> Tours:
    """ILS for the most places (first keeping the no-two-food-in-a-row rule, then without it if
    that fits more), then the balancing local search until the time limit."""
    deadline = clock.perf_counter() + time_limit_s
    ruled = prep.instance

    def score(tours: Tours) -> int:
        return sum(ruled.nodes[j].score for tour in tours for j in tour)

    tours, _ = ils.solve(ruled, time_limit_s / 2, 0, STALL_S)
    used = ruled
    if score(tours) < sum(node.score for node in ruled.nodes):
        free = replace(ruled, no_adjacent=frozenset())
        loose, _ = ils.solve(free, max(0.0, deadline - clock.perf_counter()), 0, STALL_S)
        if score(loose) > score(tours):
            tours, used = loose, free
    tours = ils.rebalance(used, tours, plan_cost(prep), deadline)
    check = validate(ruled, tours)
    if not check.feasible:  # never hand out a plan that misses a window or the end of a day
        raise ProblemError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "routing-invalid-plan",
            "Invalid itinerary",
            f"The solver produced an infeasible plan: {check.errors[0]}",
        )
    return tours


def outcome(prep: Prepared, tours: Tours) -> tuple[list[Unscheduled], list[str]]:
    """Every place the plan leaves out (in request order) and the warnings."""
    inst = prep.instance
    visited = {j for tour in tours for j in tour}
    unscheduled = prep.unscheduled + [
        Unscheduled(poi=card, reason="no_time")
        for j, (card, _) in enumerate(prep.located, start=1)
        if j not in visited
    ]
    order = {place.id: i for i, place in enumerate(prep.request.places)}
    unscheduled.sort(key=lambda item: order[item.poi.id])

    warnings = ["travel_estimated"] if prep.estimated else []
    warnings += [
        f"hours_unknown:{card.id}"
        for card, poi in prep.located
        if weekly_intervals(poi.opening_hours) is None
    ]
    warnings += RULES.broken(*prep.day_categories(tours))[1]
    for d, tour in enumerate(tours):
        tags = dict.fromkeys(inst.nodes[a].tag for a, b in pairwise(tour) if inst.clash(a, b))
        warnings += [f"not_adjacent:{tag}:{prep.dates[d]}" for tag in tags]
    return unscheduled, warnings


def meta(prep: Prepared, settings: Settings) -> RoutingMeta:
    return RoutingMeta(
        routing_version=ROUTING_VERSION,
        visit_minutes_version=VISIT_MINUTES.version,
        rules_version=RULES.version,
        travel_source="osrm" if settings.osrm_url and not prep.estimated else "estimated",
        latency_ms=round((clock.perf_counter() - prep.started) * 1000),
    )


async def plan(
    conn: AsyncConnection,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> RoutingResponse:
    prep = await prepare(conn, client, settings, request)
    tours = await asyncio.to_thread(solve, prep, settings.routing_time_limit_seconds)
    inst, km = prep.instance, prep.km
    origin = datetime.combine(request.start_at.date(), time(), TRIP_TZ)

    def at(seconds: int) -> datetime:
        return origin + timedelta(seconds=seconds)

    days = []
    for d, tour in enumerate(tours):
        moment, here, stops = inst.tours[d][0], 0, []
        for node_id in tour:
            node = inst.nodes[node_id]
            arrive = moment + inst.travel[here][node_id]
            start = node.start_at(arrive)[0]  # solve() validated the plan
            stops.append(
                Stop(
                    poi=prep.located[node_id - 1][0],
                    arrive=at(arrive),
                    start=at(start),
                    leave=at(start + node.service),
                    wait_minutes=round((start - arrive) / 60),
                    visit_minutes=node.service // 60,
                    leg=leg(km[here][node_id]),
                )
            )
            moment, here = start + node.service, node_id
        back = leg(km[here][0]) if tour else None
        days.append(Day(date=prep.dates[d], stops=stops, return_leg=back))
    unscheduled, warnings = outcome(prep, tours)
    return RoutingResponse(
        days=days, unscheduled=unscheduled, warnings=warnings, meta=meta(prep, settings)
    )


async def plan_days(
    conn: AsyncConnection,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> DaysResponse:
    prep = await prepare(conn, client, settings, request)
    tours = await asyncio.to_thread(solve, prep, settings.routing_time_limit_seconds)
    days = []
    for d, tour in enumerate(tours):
        begin, end = prep.instance.tours[d]
        days.append(
            DayPlan(
                date=prep.dates[d],
                places=[prep.located[j - 1][0] for j in tour],
                busy_minutes=round(busy_seconds(prep.instance, tour)[1] / 60),
                capacity_minutes=round((end - begin) / 60),
            )
        )
    unscheduled, warnings = outcome(prep, tours)
    return DaysResponse(
        days=days,
        unscheduled=unscheduled,
        open_dates=prep.open_dates,
        warnings=warnings,
        meta=meta(prep, settings),
    )
