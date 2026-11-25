"""POST /routing and /routing/days: spread the traveller's places over the trip days and order
each day (ILS on a TOPTW, then a balancing local search).

Every day starts and ends at the traveller's location. Times are integer seconds on one trip
timeline that starts at midnight of the first trip day (Asia/Ho_Chi_Minh). A place's windows are
its opening hours on each trip day, clipped to that day and shortened by the visit, so the visit
ends before closing; the tour of day d can only use day d's windows, and a pinned place keeps
only the windows of its day.

Plans are compared by, in order: places visited (must-visit first), broken travel rules
(config/rules.yaml), day balance beyond the tolerance (the spread, then how uneven the days
are), lost time (travel, waiting, and the soft rules' penalties). The order of each day is
judged by broken rules and lost time alone.
"""

import asyncio
import math
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from itertools import pairwise, permutations

import httpx
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
from app.services.routing.rules import DINNER, LUNCH, RULES
from app.services.routing.validate import Tours, tour_errors, validate
from app.services.routing.visits import VISIT_MINUTES

ROUTING_VERSION = "routing-v2"  # v2: day balance, travel rules, pins
DAY_S = 24 * 3600
WEEK_S = 7 * DAY_S
WALK_THRESHOLD_KM = 1.0  # agreed: walk below 1 km of road distance, ride otherwise
WALK_KMH, RIDE_KMH = 4.5, 20.0  # agreed speeds; 20 km/h is a city-traffic motorbike average
DETOUR_FACTOR = 1.3  # road / straight-line distance, used when OSRM gives no distance
# Stop the ILS after this many shakes without a better plan (one restart cycle). A count, so the
# plan does not depend on the server's load; the time limit stays as the backstop. On 50 real
# places over 1-7 days, 300 shakes found the same scores in twice the time.
STALL_SHAKES = ils.MAX_NO_IMPROVE


def day_bounds(request: RoutingRequest) -> list[Window]:
    """(begin, end) of each trip day on the trip timeline. A day that ends the next day ends
    after DAY_S; the next day starts at day_start, later than that end, so days never overlap."""
    first, last = request.start_at, request.end_at
    days = (request.last_day - first.date()).days + 1
    day_start, day_end = to_minutes(request.day_start) * 60, to_minutes(request.day_end) * 60
    if request.overnight:
        day_end += DAY_S
    # An end after midnight up to RoutingRequest.day_close is the end of the day before.
    last_end = (last.date() - request.last_day).days * DAY_S + _seconds(last)
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


def night_tail(request: RoutingRequest) -> int:
    """Seconds after midnight that still belong to the day before: day_end when days end the
    next day, else 0."""
    return to_minutes(request.day_end) * 60 if request.overnight else 0


def trip_day(moment: int, tail: int = 0) -> int:
    """The trip day a moment of the trip timeline is in; the hours after midnight up to `tail`
    (night_tail) are the day before's."""
    return max(0, (moment - tail) // DAY_S)


def day_clock(moment: int, tail: int = 0) -> int:
    """Seconds after midnight of the moment's own trip day: 00:30 of a day that ends the next
    day is 24:30."""
    return moment - trip_day(moment, tail) * DAY_S


def windows(
    hours: WeeklyHours | None, service: int, bounds: list[Window], first_weekday: int
) -> tuple[Window, ...]:
    """Start-of-visit windows on the trip timeline; no hours on record = open all day."""
    intervals = weekly_intervals(hours)
    found = []
    for d, (begin, end) in enumerate(bounds):
        weekday = (first_weekday + d) % 7
        shift = d * DAY_S - weekday * DAY_MIN * 60  # week seconds -> trip seconds of day d
        # The next week's hours too: a Sunday that ends the next day runs into Monday
        opened = (
            [(begin, end)]
            if intervals is None
            else [
                (start * 60 + base, stop * 60 + base)
                for base in (shift, shift + WEEK_S)
                for start, stop in intervals
            ]
        )
        for start, stop in _across_midnight(opened):
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


def _across_midnight(opened: list[Window]) -> list[Window]:
    """Opening intervals in order, one that ends at midnight joined to one from that midnight:
    open until 24:00 and again from 00:00 (or round the clock) is a visit across midnight."""
    joined: list[list[int]] = []
    for start, stop in sorted(opened):
        if joined and start == joined[-1][1] and start % DAY_S == 0:
            joined[-1][1] = stop
        else:
            joined.append([start, stop])
    return [(start, stop) for start, stop in joined]


def not_before(slots: tuple[Window, ...], at: int, tail: int = 0) -> tuple[Window, ...]:
    """The windows with every start moved to `at` (seconds after midnight) of its own trip day
    (trip_day) or later; a window that closes before then is dropped."""
    moved = ((max(first, trip_day(first, tail) * DAY_S + at), last) for first, last in slots)
    return tuple(window for window in moved if window[0] <= window[1])


def within(slots: tuple[Window, ...], spans: tuple[tuple[int, int], ...]) -> tuple[Window, ...]:
    """The parts of the windows that start inside one of the daily spans (seconds after
    midnight, the end excluded to the minute)."""
    found = []
    for first, last in slots:
        day = first // DAY_S * DAY_S
        for begin, end in spans:
            low, high = max(first, day + begin), min(last, day + end - 60)
            if low <= high:
                found.append((low, high))
    return tuple(sorted(found))


def meal_slots(groups: list[list[int]], slots: dict[int, tuple[Window, ...]]) -> dict[int, int]:
    """node -> meal window (position in RULES.meal_windows) for each day's group of pinned
    meals, in request order: as many meals as possible get a window of their own, the windows
    are taken in RULES.slot_order, and earlier meals in the request take earlier windows. A meal
    can take a window only if its own start windows meet it."""
    found: dict[int, int] = {}
    order = RULES.slot_order
    for group in groups:
        fits = {j: {w for w in order if within(slots[j], (RULES.meal_windows[w],))} for j in group}
        best: tuple[tuple[int, int, list[int]], dict[int, int]] | None = None
        for count in range(min(len(group), len(order)), 0, -1):
            for chosen in permutations(group, count):
                for taken in permutations(order, count):
                    if any(w not in fits[j] for j, w in zip(chosen, taken, strict=True)):
                        continue
                    pairs = sorted(zip(chosen, taken, strict=True), key=lambda p: group.index(p[0]))
                    swapped = sum(a[1] > b[1] for k, a in enumerate(pairs) for b in pairs[k + 1 :])
                    rank = sum(order.index(w) for w in taken)  # lunch, then dinner, then breakfast
                    key = (rank, swapped, [group.index(j) for j in chosen])
                    if best is None or key < best[0]:
                        best = (key, dict(zip(chosen, taken, strict=True)))
            if best is not None:  # the most meals that can each have a window
                found.update(best[1])
                break
    return found


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
    # The same instance with opening hours only (no not_before rule); None when no place has one.
    loose: Instance | None = None
    late: tuple[tuple[str, int] | None, ...] = ()  # per node: (rule node, seconds after midnight)
    slot: tuple[int | None, ...] = ()  # per node: the meal window a pinned meal takes, or None
    # The instance before the meal slots (not_before kept); None when no meal has one.
    unslotted: Instance | None = None

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
    tail = night_tail(request)
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
        # A length the traveller set (sent on the card) comes before the place's own
        service = (card.visit_minutes or VISIT_MINUTES.of_place(poi)) * 60
        slots = windows(poi.opening_hours, service, bounds, request.start_at.weekday())
        open_days = sorted({trip_day(start, tail) for start, _ in slots})  # one day per window
        open_dates[card.id] = [first_day + timedelta(days=d) for d in open_days]
        if card.id in pinned:
            slots = tuple(w for w in slots if trip_day(w[0], tail) == pinned[card.id])
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

    # A pinned place outweighs every free place together, a must-visit every pinned one
    pin_score = len(located) + 1
    must_score = pin_score * (len(pinned) + 1)
    nodes = [Node(request.start.lat, request.start.lon, 0, 0, ((bounds[0][0], bounds[-1][1]),))]
    nodes += [
        Node(
            poi.latitude,
            poi.longitude,
            service,
            must_score if card.id in must else pin_score if card.id in pinned else 1,
            slots,
            RULES.tag(poi.category),
        )
        for card, poi, service, slots in located
    ]
    # A place with a not_before rule keeps only the part of its windows after that time; when
    # nothing is left (it closes earlier, or the day ends first) its opening hours stand.
    late = (None, *(RULES.earliest(poi.category) for _, poi, *_ in located))
    ruled = [
        replace(node, windows=not_before(node.windows, rule[1], tail) or node.windows)
        if rule
        else node
        for node, rule in zip(nodes, late, strict=True)
    ]
    # A meal pinned to a day takes one meal window of that day for itself (RULES.slot_order).
    # A meal left without one keeps its hours; a second meal in a used window is off-hours.
    # Unpinned meals get none: /routing/days chooses their days.
    unslotted = list(ruled)
    slot: list[int | None] = [None] * len(nodes)
    groups: dict[int, list[int]] = {}
    for j, (card, poi, *_) in enumerate(located, start=1):
        if card.id in pinned and RULES.has_slot(poi.category):
            groups.setdefault(pinned[card.id], []).append(j)
    windows_of = {j: nodes[j].windows for group in groups.values() for j in group}
    taken = meal_slots(list(groups.values()), windows_of)
    for j, window in taken.items():
        slot[j] = window
        ruled[j] = replace(
            ruled[j], windows=within(nodes[j].windows, (RULES.meal_windows[window],))
        )
    km, estimated = await distances(client, settings, [(n.x, n.y) for n in nodes])
    travel = tuple(tuple(leg_seconds(value) for value in row) for row in km)
    loose = Instance("request", tuple(nodes), travel, tuple(bounds), frozenset(RULES.not_adjacent))
    instance = replace(loose, nodes=tuple(ruled))
    return Prepared(
        request,
        instance,
        [(card, poi) for card, poi, *_ in located],
        unscheduled,
        open_dates,
        km,
        estimated,
        started,
        loose if unslotted != nodes else None,
        late,
        tuple(slot),
        replace(loose, nodes=tuple(unslotted)) if taken else None,
    )


def busy_seconds(inst: Instance, tour: list[int]) -> tuple[int, int]:
    """(travel, travel + visits) of one day, from and back to the start."""
    moving = sum(inst.travel[a][b] for a, b in pairwise([0, *tour, 0])) if tour else 0
    return moving, moving + sum(inst.nodes[j].service for j in tour)


def early_visits(prep: Prepared, inst: Instance, tour: list[int], day: int) -> list[str]:
    """The not_before nodes whose visit on this day starts before its time (one per visit)."""
    if not any(prep.late[j] for j in tour):
        return []
    found = []
    moment, here = inst.tours[day][0], 0
    for j in tour:
        slot = inst.nodes[j].start_at(moment + inst.travel[here][j])
        if slot is None:  # an infeasible trial tour: the caller rejects it
            return found
        rule = prep.late[j]
        if rule and slot[0] < day * DAY_S + rule[1]:
            found.append(rule[0])
        moment, here = slot[0] + inst.nodes[j].service, j
    return found


def day_step(prep: Prepared) -> Callable[[int, int, int, bool, int], tuple[int, int]]:
    """(node, arrival, start of its visit, first visit of the day, state) -> (lost seconds of
    this visit, state after it). Lost: the wait (free time before the evening taken out), plus
    the penalties of the soft rules. The state starts at 0 each day and holds the meal windows
    used so far (one bit each), whether the evening has begun and whether a daytime visit came
    after lunch."""
    meals = [False, *(RULES.is_meal(poi.category) for _, poi in prep.located)]
    avoid = [False, *(RULES.is_avoided(poi.category) for _, poi in prep.located)]
    evening = 1 << max(len(RULES.meal_windows), DINNER + 1)
    after_lunch = evening << 1
    tail = night_tail(prep.request)

    def step(j: int, arrive: int, start: int, first: bool, state: int) -> tuple[int, int]:
        at = day_clock(start, tail) if tail else start % DAY_S  # past midnight: still tonight
        # A day starts when its first place opens, but a meal put first to wait for its slot
        # loses that time: the day would open with a lunch waiting for 11:00.
        lost = 0 if first and prep.slot[j] is None else start - arrive
        if RULES.free_from is not None and at >= RULES.free_from:
            # Waiting for the evening is the traveller's own time, up to free_allow (twice that
            # when a daytime visit after lunch lets the rest pass split it)
            lost = max(0, lost - RULES.free_allow * (2 if state & after_lunch else 1))
        if state & evening and prep.late[j] is None:
            lost += RULES.after_penalty  # a meal or a daytime visit after the evening began
        if meals[j]:
            window = RULES.meal_window(at)
            if window is None or (RULES.meal_one_per_window and state >> window & 1):
                lost += RULES.meal_penalty
            else:
                state |= 1 << window | (evening if window == DINNER else 0)
        elif prep.late[j] is not None:
            state |= evening
        elif state >> LUNCH & 1 or at >= RULES.rest_from:
            state |= after_lunch
        if avoid[j] and RULES.avoid_at(at):
            lost += RULES.avoid_penalty
        return lost, state

    return step


def plan_cost(
    prep: Prepared, used: Instance | None = None
) -> Callable[[Tours], tuple[int, int, int, int]]:
    """(broken rules, balance points beyond the tolerance, unevenness beyond it, lost time):
    lower is better. Unevenness (sum of squared deviations from the mean load) also drops when a
    place moves to one of several empty days, which leaves the spread unchanged; within the
    tolerance it is 0, so lost time decides: seconds of travel, of waiting after the day's first
    visit (free time before the evening aside), and the soft rules' penalties (day_step)."""
    inst = prep.instance
    timed = used or inst  # the windows the plan was made with
    capacity = [end - begin for begin, end in inst.tours]
    active = [d for d, length in enumerate(capacity) if length > 0]  # as in day_categories
    categories = [None] + [poi.category for _, poi in prep.located]
    step = day_step(prep)

    # Per tour, cached: a trial changes one or two tours, the others are looked up.
    @lru_cache(maxsize=65_536)
    def day(tour: tuple[int, ...]) -> tuple[int, int, int, tuple[int, ...]]:
        moving, busy = busy_seconds(inst, list(tour))
        clashes = inst.clashes([list(tour)])
        return moving, busy, clashes, RULES.counts([categories[j] for j in tour])

    @lru_cache(maxsize=65_536)
    def early(tour: tuple[int, ...], d: int) -> int:
        return len(early_visits(prep, timed, list(tour), d))

    @lru_cache(maxsize=65_536)
    def idle(tour: tuple[int, ...], d: int) -> int:
        """The day's lost time other than travel (day_step), on the plan's own windows."""
        total, moment, here, state = 0, timed.tours[d][0], 0, 0
        for k, j in enumerate(tour):
            arrive = moment + timed.travel[here][j]
            slot = timed.nodes[j].start_at(arrive)
            if slot is None:  # an infeasible trial tour: the caller rejects it
                return total
            lost, state = step(j, arrive, slot[0], not k, state)
            total += lost
            moment, here = slot[0] + timed.nodes[j].service, j
        return total

    def cost(tours: Tours) -> tuple[int, int, int, int]:
        days = [day(tuple(tour)) for tour in tours]
        broken = sum(over for over, _, _ in RULES.overs([days[d][3] for d in active]))
        broken += sum(clashes for _, _, clashes, _ in days)
        if prep.loose is not None:
            broken += sum(early(tuple(tour), d) for d, tour in enumerate(tours))
        lost = sum(moving for moving, _, _, _ in days)
        lost += sum(idle(tuple(tour), d) for d, tour in enumerate(tours))
        loads = [days[d][1] / capacity[d] for d in active]
        spread = max(loads) - min(loads) if loads else 0.0
        beyond = max(0, math.ceil(round((spread - RULES.balance_tolerance) * 100, 6)))
        mean = sum(loads) / len(loads) if loads else 0.0
        uneven = round(sum((load - mean) ** 2 for load in loads) * 10_000) if beyond else 0
        return broken, beyond, uneven, lost

    return cost


def pinned_start(prep: Prepared, inst: Instance | None = None) -> Tours:
    """Each day's pinned places in request order, where that order is feasible. /routing/days
    lists places in visiting order, so a split sent back as pins starts from that plan."""
    inst, first = inst or prep.instance, prep.request.start_at.date()
    day_of = {pin.id: (pin.date - first).days for pin in prep.request.pins}
    tours: Tours = [[] for _ in range(inst.m)]
    for j, (card, _) in enumerate(prep.located, start=1):
        if card.id in day_of:
            tours[day_of[card.id]].append(j)
    return [[] if tour_errors(inst, tour, d) else tour for d, tour in enumerate(tours)]


def solve(prep: Prepared, time_limit_s: float) -> tuple[Tours, Instance]:
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
    total = sum(node.score for node in ruled.nodes)
    # While a place is left out a rule gives way: first no-two-food-in-a-row, then the meal
    # slots, then not_before.
    relaxed = [replace(ruled, no_adjacent=frozenset())]
    if prep.unslotted is not None:
        relaxed.append(replace(prep.unslotted, no_adjacent=frozenset()))
    if prep.loose is not None:
        relaxed.append(replace(prep.loose, no_adjacent=frozenset()))
    for free in relaxed:
        if score(tours) >= total:
            break
        # each pass may use all the time left: the not_before pass only runs if the first one
        # still leaves a place out, and it should not cost the first pass its search time
        left = max(0.0, deadline - clock.perf_counter())
        # pins checked against the windows this pass uses (the not_before pass has wider ones)
        begin = start if free.nodes is ruled.nodes else pinned_start(prep, free)
        loose, _ = ils.solve(free, left, 0, STALL_SHAKES, begin)
        if score(loose) > score(tours):
            tours, used = loose, free
    if used is not ruled:
        # A rule gives way only for the day that needs it. From the strictest windows to the
        # wider ones, a day that can be ordered in stricter windows takes that order and its
        # places get those windows back; only a day that cannot keeps the wider ones.
        nodes, kept = list(used.nodes), [False] * len(tours)
        for strict in relaxed:
            if strict.nodes is used.nodes:
                break
            for d, tour in enumerate(tours):
                if kept[d]:
                    continue
                if len(tour) <= ils.EXACT_MAX:
                    order = ils.exact_order(strict, d, list(tour))
                else:
                    order = None if tour_errors(strict, list(tour), d) else list(tour)
                if order is not None:
                    tours[d], kept[d] = order, True
                    for j in order:
                        nodes[j] = strict.nodes[j]
        used = replace(used, nodes=tuple(nodes))
    cost = plan_cost(prep, used)
    tours = ils.rebalance(used, tours, cost, deadline)
    # Each round adds at least one place and never drops one: at most one per place.
    while (filled := ils.fill(used, tours, cost, deadline)) != tours:
        tours = ils.rebalance(used, filled, cost, deadline)
    step = day_step(prep)
    tail = night_tail(prep.request)

    def order_penalty(j: int, arrive: int, start: int, first: bool, state: int) -> tuple[int, int]:
        # a bar before its evening breaks a rule, which outweighs any amount of lost time; a
        # visit after midnight on a day that ends the next day is still that evening
        rule = prep.late[j] if prep.loose is not None else None
        lost, state = step(j, arrive, start, first, state)
        early = rule and day_clock(start, tail) < rule[1]
        return lost + (10 * DAY_S if early else 0), state

    def order_cost(plan: Tours) -> tuple[int, int]:
        # Another order of a day moves no place to another day, so the day balance is no reason
        # to keep a longer order: broken rules, then lost time.
        broken, _, _, lost = cost(plan)
        return broken, lost

    tours = ils.polish(used, tours, order_cost, deadline, prep.instance.clash, order_penalty)
    check = validate(used, tours)
    if not check.feasible:  # never hand out a plan that misses a window or the end of a day
        raise ProblemError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "routing-invalid-plan",
            "Invalid itinerary",
            f"The solver produced an infeasible plan: {check.errors[0]}",
        )
    return tours, used


def outcome(
    prep: Prepared, tours: Tours, used: Instance | None = None
) -> tuple[list[Unscheduled], list[str]]:
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
        early = dict.fromkeys(early_visits(prep, used or inst, tour, d))
        warnings += [f"not_before:{node}:{prep.dates[d]}" for node in early]
    return unscheduled, warnings


def holds(prep: Prepared, inst: Instance, d: int, tour: list[int]) -> dict[int, int]:
    """{position: earliest start}: where the day's free time goes (RULES midday_rest). Only
    start times move, inside the slack the plan already has; places and order stay.

    The visits before a lunch that waits for its window start later. The afternoon (the visit
    after the last lunch, or the first from rest_from on a day without one) starts later by
    the room before its anchor, at most rest_max: the anchor is the first later visit that
    waits from free_from on (the evening meal or venue), else the first that waits at all (a
    place reopening). With no anchor, the afternoon starts after a rest set by the day's stop
    count. A daytime visit never moves to end after free_from; a waiting visit does not move,
    except an outdoor afternoon visit that waits out avoid hours: its whole wait from arrival
    stays within rest_max."""
    if not (RULES.rest_max or RULES.late_start or RULES.rest_by_stops) or len(tour) < 2:
        return {}
    base, (begin, end) = d * DAY_S, inst.tours[d]
    moment, here = begin, 0
    arrive: list[int] = []
    start: list[int] = []
    close: list[int] = []  # the latest start each visit may move to
    for j in tour:
        node = inst.nodes[j]
        reach = moment + inst.travel[here][j]
        first, last = node.start_at(reach)  # solve() validated the plan
        if RULES.free_from is not None and prep.late[j] is None and prep.slot[j] is None:
            last = min(last, max(first, base + RULES.free_from - node.service))
        arrive.append(reach)
        start.append(first)
        close.append(last)
        moment, here = first + node.service, j
    spare = end - (moment + inst.travel[here][0])  # how much later the day may end

    def room(first: int, anchor: int) -> int:
        """How much later visit `first` may start with `anchor` (or the day's end) unchanged."""
        slack = spare if anchor == len(tour) else 0
        for k in range(anchor - 1, first - 1, -1):
            after = slack if k + 1 == len(tour) else start[k + 1] - arrive[k + 1] + slack
            slack = min(close[k] - start[k], after)
        return max(0, slack)

    found: dict[int, int] = {}
    lunch = RULES.meal_windows[LUNCH]
    lunches = [
        k
        for k, j in enumerate(tour)
        if RULES.is_meal(prep.located[j - 1][1].category) and lunch[0] <= start[k] - base < lunch[1]
    ]
    first_lunch = lunches[0] if lunches else 0
    late = RULES.late_start and first_lunch > 0 and start[first_lunch] > arrive[first_lunch]
    if late and (shift := room(0, first_lunch)) > 0:
        found[0] = start[0] + shift
    afternoon = (
        lunches[-1] + 1
        if lunches
        else next((k for k, at in enumerate(start) if at - base >= RULES.rest_from), len(tour))
    )
    if afternoon >= len(tour):
        return found
    waits = [k for k in range(max(afternoon, 1), len(tour)) if start[k] > arrive[k]]
    evenings = [
        k
        for k in waits
        if k > afternoon and RULES.free_from is not None and start[k] - base >= RULES.free_from
    ]
    anchor = (evenings or waits or [None])[0]
    shift = 0
    if anchor is not None:
        shift = min(room(afternoon, anchor), RULES.rest_max) if anchor > afternoon else 0
    elif RULES.rest_by_stops and afternoon > 0:
        rest = next((length for stops, length in RULES.rest_by_stops if len(tour) <= stops), 0)
        shift = min(room(afternoon, len(tour)), rest)
    # An outdoor sight after the rest still starting in avoid hours waits them out when the day
    # has room, so the rest does not depend on how long lunch took.
    at = start[afternoon] + shift - base
    if (
        RULES.rest_max
        and afternoon > 0
        and RULES.is_avoided(prep.located[tour[afternoon] - 1][1].category)
    ):
        until = next((end for begin, end in RULES.avoid_windows if begin <= at < end), None)
        limit = room(afternoon, anchor if anchor is not None and anchor > afternoon else len(tour))
        if (
            until is not None
            and until - (start[afternoon] - base) <= limit
            and base + until - arrive[afternoon] <= RULES.rest_max
        ):
            shift = until - (start[afternoon] - base)
    if shift > 0:
        found[afternoon] = start[afternoon] + shift
    return found


def held_ok(inst: Instance, d: int, tour: list[int], hold: dict[int, int]) -> bool:
    """The day with the held starts still meets every start window and the end of the day."""
    moment, here = inst.tours[d][0], 0
    for pos, j in enumerate(tour):
        slot = inst.nodes[j].start_at(max(moment + inst.travel[here][j], hold.get(pos, 0)))
        if slot is None:
            return False
        moment, here = slot[0] + inst.nodes[j].service, j
    return moment + inst.travel[here][0] <= inst.tours[d][1]


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
    tours, inst = await asyncio.to_thread(solve, prep, settings.routing_time_limit_seconds)
    km = prep.km
    origin = datetime.combine(request.start_at.date(), time(), TRIP_TZ)

    def at(seconds: int) -> datetime:
        return origin + timedelta(seconds=seconds)

    days = []
    for d, tour in enumerate(tours):
        moment, here, stops = inst.tours[d][0], 0, []
        hold = holds(prep, inst, d, tour)
        if hold and not held_ok(inst, d, tour, hold):
            hold = {}  # never hand out a plan that misses a window: keep the plain times
        for pos, node_id in enumerate(tour):
            node = inst.nodes[node_id]
            arrive = moment + inst.travel[here][node_id]
            start = node.start_at(max(arrive, hold.get(pos, 0)))[0]  # checked above
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
    unscheduled, warnings = outcome(prep, tours, inst)
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
    tours, used = await asyncio.to_thread(solve, prep, settings.routing_time_limit_seconds)
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
    unscheduled, warnings = outcome(prep, tours, used)
    return DaysResponse(
        days=days,
        unscheduled=unscheduled,
        open_dates=prep.open_dates,
        warnings=warnings,
        meta=meta(prep, settings),
    )
