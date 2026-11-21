"""POST /routing and /routing/days: spread the traveller's places over the trip days and order
each day (ILS on a TOPTW, then a balancing local search).

Every day starts and ends at the traveller's location. Times are integer seconds on one trip
timeline that starts at midnight of the first trip day (Asia/Ho_Chi_Minh). A place's windows are
its opening hours on each trip day, clipped to that day and shortened by the visit, so the visit
ends before closing; the tour of day d can only use day d's windows, and a pinned place keeps
only the windows of its day.

Plans are compared by, in order: places visited (must-visit first), broken travel rules
(config/rules.yaml), day balance beyond the tolerance (the spread, then how uneven the days
are), total travel.
"""

import asyncio
import math
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

import httpx
import yaml
from fastapi import status

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
from app.services.retrieval.db import Connect
from app.services.retrieval.hours import DAY_MIN, WeeklyHours, to_minutes, weekly_intervals
from app.services.retrieval.poi import Poi
from app.services.retrieval.scoring import haversine_km
from app.services.routing import ils
from app.services.routing.instance import Instance, Node, Window
from app.services.routing.osrm import road_km
from app.services.routing.rules import RULES
from app.services.routing.validate import Tours, tour_errors, validate

ROUTING_VERSION = "routing-v2"  # v2: day balance, travel rules, pins
DAY_S = 24 * 3600
WALK_THRESHOLD_KM = 1.0  # agreed: walk below 1 km of road distance, ride otherwise
WALK_KMH, RIDE_KMH = 4.5, 20.0  # agreed speeds; 20 km/h is a city-traffic motorbike average
DETOUR_FACTOR = 1.3  # road / straight-line distance, used when OSRM gives no distance
# Stop the ILS after this many shakes without a better plan (one restart cycle). A count, so the
# plan does not depend on the server's load; the time limit stays as the backstop. On 50 real
# places over 1-7 days, 300 shakes found the same scores in twice the time.
STALL_SHAKES = ils.MAX_NO_IMPROVE
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
    days = (request.last_day - first.date()).days + 1
    day_start, day_end = to_minutes(request.day_start) * 60, to_minutes(request.day_end) * 60
    # An end at midnight is the end of the day before (RoutingRequest.last_day).
    last_end = _seconds(last) if last.date() == request.last_day else DAY_S
    bounds = []
    for d in range(days):
        begin = d * DAY_S + (_seconds(first) if d == 0 else day_start)
        end = d * DAY_S + day_end
        if d == days - 1:
            end = min(end, d * DAY_S + last_end)
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
        dates = self.dates
        return days, [dates[d] for d in active]


async def prepare(
    connect: Connect,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> Prepared:
    started = clock.perf_counter()
    async with connect() as conn:  # released before OSRM and the solver
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
            # Open on its day(s) but not inside the traveller's hours: the day is too short.
            whole = [(d * DAY_S, (d + 1) * DAY_S) for d in range(len(bounds))]
            opened = windows(poi.opening_hours, service, whole, request.start_at.weekday())
            if any(card.id not in pinned or w[0] // DAY_S == pinned[card.id] for w in opened):
                reason = "no_time"
            else:
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


def plan_cost(prep: Prepared) -> Callable[[Tours], tuple[int, int, int, int]]:
    """(broken rules, balance points beyond the tolerance, unevenness beyond it, travel seconds):
    lower is better. Unevenness (sum of squared deviations from the mean load) also drops when a
    place moves to one of several empty days, which leaves the spread unchanged; within the
    tolerance it is 0, so travel decides."""
    inst = prep.instance
    capacity = [end - begin for begin, end in inst.tours]
    active = [d for d, length in enumerate(capacity) if length > 0]  # as in day_categories
    categories = [None] + [poi.category for _, poi in prep.located]

    # Per tour, cached: a trial changes one or two tours, the others are looked up.
    @lru_cache(maxsize=65_536)
    def day(tour: tuple[int, ...]) -> tuple[int, int, int, tuple[int, ...]]:
        moving, busy = busy_seconds(inst, list(tour))
        clashes = inst.clashes([list(tour)])
        return moving, busy, clashes, RULES.counts([categories[j] for j in tour])

    def cost(tours: Tours) -> tuple[int, int, int, int]:
        days = [day(tuple(tour)) for tour in tours]
        broken = sum(over for over, _, _ in RULES.overs([days[d][3] for d in active]))
        broken += sum(clashes for _, _, clashes, _ in days)
        total_travel = sum(moving for moving, _, _, _ in days)
        loads = [days[d][1] / capacity[d] for d in active]
        spread = max(loads) - min(loads) if loads else 0.0
        beyond = max(0, math.ceil(round((spread - RULES.balance_tolerance) * 100, 6)))
        mean = sum(loads) / len(loads) if loads else 0.0
        uneven = round(sum((load - mean) ** 2 for load in loads) * 10_000) if beyond else 0
        return broken, beyond, uneven, total_travel

    return cost


def pinned_start(prep: Prepared) -> Tours:
    """Each day's pinned places in request order, where that order is feasible. /routing/days
    lists places in visiting order, so a split sent back as pins starts from that plan."""
    inst, first = prep.instance, prep.request.start_at.date()
    day_of = {pin.id: (pin.date - first).days for pin in prep.request.pins}
    tours: Tours = [[] for _ in range(inst.m)]
    for j, (card, _) in enumerate(prep.located, start=1):
        if card.id in day_of:
            tours[day_of[card.id]].append(j)
    return [[] if tour_errors(inst, tour, d) else tour for d, tour in enumerate(tours)]


def solve(prep: Prepared, time_limit_s: float) -> Tours:
    """ILS for the most places (first keeping the no-two-food-in-a-row rule, then without it if
    that fits more), then the balancing local search, alternating with inserting what still
    fits, until nothing changes or the time limit."""
    deadline = clock.perf_counter() + time_limit_s
    ruled = prep.instance
    start = pinned_start(prep)

    def score(tours: Tours) -> int:
        return sum(ruled.nodes[j].score for tour in tours for j in tour)

    tours, _ = ils.solve(ruled, time_limit_s / 2, 0, STALL_SHAKES, start)
    used = ruled
    if score(tours) < sum(node.score for node in ruled.nodes):
        free = replace(ruled, no_adjacent=frozenset())
        left = max(0.0, deadline - clock.perf_counter())
        loose, _ = ils.solve(free, left, 0, STALL_SHAKES, start)
        if score(loose) > score(tours):
            tours, used = loose, free
    cost = plan_cost(prep)
    tours = ils.rebalance(used, tours, cost, deadline)
    # Each round adds at least one place and never drops one: at most one per place.
    while (filled := ils.fill(used, tours, cost, deadline)) != tours:
        tours = ils.rebalance(used, filled, cost, deadline)
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
    connect: Connect,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> RoutingResponse:
    prep = await prepare(connect, client, settings, request)
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
    connect: Connect,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RoutingRequest,
) -> DaysResponse:
    prep = await prepare(connect, client, settings, request)
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
