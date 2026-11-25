"""Days that end the next day: a day_end at or before day_start (09:00-03:00 is an 18 h day).
At most 05:00, at most 20 h on a one-day trip and 18 h on a longer one (6 h of rest)."""

from datetime import date
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from app.api.routes.retrieval import get_connection
from app.main import app
from app.schemas.routing import RoutingRequest
from app.services.routing import service
from tests.test_routing_api import (  # noqa: F401 (client is a fixture)
    BODY,
    START,
    H,
    card,
    client,
    no_connection,
    plan,
    poi,
    request,
)
from tests.test_routing_days import days_plan

D = service.DAY_S
FRI, SUN = 4, 6  # 2026-11-20 is a Friday
WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BAR = "entertainment.nightlife"
NIGHT = {"day_start": "09:00", "day_end": "03:00"}  # 18 h


def timed(minutes: dict[str, int], start: str, end: str, **extra):
    """A request whose cards carry their visit length."""
    places = [card(poi_id) | {"visit_minutes": m} for poi_id, m in minutes.items()]
    return RoutingRequest.model_validate(
        {"places": places, "start": START, "start_at": start, "end_at": end} | extra
    )


def clock(stop) -> str:
    return stop.start.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- request


@pytest.mark.parametrize(
    ("start", "end", "window", "days"),
    [
        ("2026-11-20T09:00", "2026-11-23T03:00", ("09:00", "03:00"), 3),  # 18 h, 6 h of rest
        ("2026-11-20T18:00", "2026-11-21T02:00", ("18:00", "02:00"), 1),
        ("2026-11-20T20:00", "2026-11-22T04:00", ("20:00", "04:00"), 2),
        ("2026-11-20T07:00", "2026-11-21T03:00", ("07:00", "03:00"), 1),  # 20 h on one day
        ("2026-11-20T09:00", "2026-11-21T05:00", ("09:00", "05:00"), 1),  # 05:00 is the latest
        ("2026-11-20T09:00", "2026-11-21T01:00", ("09:00", "03:00"), 1),  # ends before 03:00
        ("2026-11-20T05:59", "2026-11-22T23:59", ("05:59", "23:59"), 3),  # 18 h, same day
    ],
)
def test_a_day_that_ends_the_next_day_is_accepted(
    start: str, end: str, window: tuple[str, str], days: int
) -> None:
    body = request(["a"], start, end, day_start=window[0], day_end=window[1])
    assert len(service.day_bounds(body)) == days
    assert body.overnight == (window[1] <= window[0])


@pytest.mark.parametrize(
    ("start", "end", "window", "message"),
    [
        ("2026-11-20T09:00", "2026-11-21T06:00", ("09:00", "06:00"), "05:00 at the latest"),
        ("2026-11-20T21:00", "2026-11-21T21:00", ("21:00", "21:00"), "05:00 at the latest"),
        ("2026-11-20T06:00", "2026-11-21T05:00", ("06:00", "05:00"), "more than 20 h"),  # 23 h
        ("2026-11-20T04:00", "2026-11-21T04:00", ("04:00", "04:00"), "more than 20 h"),  # 24 h
        ("2026-11-20T06:00", "2026-11-22T05:00", ("06:00", "05:00"), "more than 18 h"),
        ("2026-11-20T07:00", "2026-11-22T03:00", ("07:00", "03:00"), "more than 18 h"),  # 20 h
        ("2026-11-20T05:00", "2026-11-22T23:30", ("05:00", "23:30"), "more than 18 h"),
        # the first day starts at start_at: 05:00 to 03:00 is 22 h
        ("2026-11-20T05:00", "2026-11-21T03:00", ("09:00", "03:00"), "more than 20 h"),
    ],
)
def test_a_day_past_the_limits_is_rejected(
    start: str, end: str, window: tuple[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        request(["a"], start, end, day_start=window[0], day_end=window[1])


def test_the_refusal_is_a_422_that_names_day_end(client: TestClient) -> None:  # noqa: F811
    app.dependency_overrides[get_connection] = no_connection
    window = {"day_start": "09:00", "day_end": "06:00"}
    response = client.post("/routing/days", json=BODY | window)
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert "day_end" in error["msg"] and "05:00 at the latest" in error["msg"]


def test_an_end_after_midnight_closes_the_night_before() -> None:
    body = request(["a"], "2026-11-20T09:00", "2026-11-23T03:00", **NIGHT)
    assert body.last_day == date(2026, 11, 22)
    early = request(["a"], "2026-11-20T09:00", "2026-11-22T02:00", **NIGHT)
    assert early.last_day == date(2026, 11, 21)
    later = request(["a"], "2026-11-20T09:00", "2026-11-22T04:00", **NIGHT)
    assert later.last_day == date(2026, 11, 22)  # after day_end: a day of its own, with no time


# ---------------------------------------------------------------- day bounds


def test_each_day_ends_the_next_day_without_overlapping_the_next() -> None:
    body = request(["a"], "2026-11-20T09:00", "2026-11-23T03:00", **NIGHT)
    bounds = service.day_bounds(body)
    assert bounds == [(9 * H, 27 * H), (D + 9 * H, D + 27 * H), (2 * D + 9 * H, 2 * D + 27 * H)]
    assert all(after[0] - before[1] == 6 * H for before, after in pairwise(bounds))  # rest


def test_the_last_night_ends_at_end_at() -> None:
    early = request(["a"], "2026-11-20T09:00", "2026-11-22T01:30", **NIGHT)
    assert service.day_bounds(early)[-1] == (D + 9 * H, D + 25.5 * H)
    midnight = request(["a"], "2026-11-20T09:00", "2026-11-22T00:00", **NIGHT)
    assert service.day_bounds(midnight) == [(9 * H, 27 * H), (D + 9 * H, 2 * D)]
    one = request(["a"], "2026-11-20T18:00", "2026-11-21T02:00", day_start="18:00", day_end="02:00")
    assert service.day_bounds(one) == [(18 * H, 26 * H)]


def test_the_hours_after_midnight_belong_to_the_night_before() -> None:
    tail = 3 * H  # days end at 03:00
    assert service.trip_day(D + 2 * H, tail) == 0 and service.trip_day(D + 9 * H, tail) == 1
    assert service.day_clock(D + 30 * 60, tail) == 24.5 * H  # 00:30 is 24:30 of that day
    assert service.trip_day(1 * H, tail) == 0  # nothing comes before the first day
    assert service.trip_day(D + 2 * H) == 1 and service.day_clock(D + 2 * H) == 2 * H  # no tail
    # a bar opening at 00:30 is already in the evening of its night
    slots = ((D + 30 * 60, D + H),)
    assert service.not_before(slots, 18 * H, tail) == slots
    assert service.not_before(slots, 18 * H) == ()  # as the next calendar day


# ---------------------------------------------------------------- opening hours


@pytest.mark.parametrize(
    ("hours", "first_weekday", "expected"),
    [
        ({"fri": [["18:00", "02:00"]]}, FRI, ((18 * H, 25 * H),)),  # bar until 02:00
        ({"sun": [["18:00", "02:00"]]}, SUN, ((18 * H, 25 * H),)),  # Sunday into Monday
        ({"fri": [["18:00", "00:00"]], "sat": [["00:00", "02:00"]]}, FRI, ((18 * H, 25 * H),)),
        ({day: [["00:00", "00:00"]] for day in WEEK}, SUN, ((9 * H, 26 * H),)),  # 24/7
        ({"fri": [["10:00", "00:00"]]}, FRI, ((10 * H, 23 * H),)),  # closes at midnight
        ({"sat": [["05:00", "10:00"]]}, FRI, ()),  # pho from 05:00: after the day ends
        ({"sat": [["00:30", "04:00"]]}, FRI, ((24.5 * H, 26 * H),)),  # opens after midnight
    ],
)
def test_windows_of_a_day_that_ends_at_three(
    hours: dict, first_weekday: int, expected: tuple
) -> None:
    assert service.windows(hours, H, [(9 * H, 27 * H)], first_weekday) == expected


# ---------------------------------------------------------------- plans


def test_a_bar_until_two_is_visited_at_half_past_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [
        poi("stall", 0.001, "food.street_food", hours={"fri": [["23:30", "00:30"]]}),
        poi("bar", 0.0011, BAR, hours={"fri": [["18:00", "02:00"]]}),
    ]
    body = timed(
        {"stall": 60, "bar": 60}, "2026-11-20T23:00", "2026-11-21T03:00",
        day_start="23:00", day_end="03:00",
    )  # fmt: skip
    result = plan(monkeypatch, pois, body)
    [day] = result.days
    assert day.date == date(2026, 11, 20)
    assert [(s.poi.id, clock(s)) for s in day.stops] == [
        ("stall", "2026-11-20 23:30"),
        ("bar", "2026-11-21 00:30"),  # after the stall closes, before the bar does
    ]
    assert day.stops[-1].leave.strftime("%H:%M") <= "02:00" and not result.unscheduled
    assert not [w for w in result.warnings if w.startswith("not_before")]  # still the evening


def test_a_place_closed_after_midnight_is_not_scheduled_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pois = [
        poi("shut", 0.002, "attraction.museum", hours={"fri": [["10:00", "00:00"]]}),
        poi("late", 0.001, "attraction.museum", hours={"sat": [["00:00", "04:00"]]}),
    ]
    body = timed(
        {"shut": 60, "late": 60}, "2026-11-20T23:30", "2026-11-21T03:00",
        day_start="23:30", day_end="03:00",
    )  # fmt: skip
    result = plan(monkeypatch, pois, body)
    assert [(u.poi.id, u.reason) for u in result.unscheduled] == [("shut", "no_time")]
    assert [(s.poi.id, clock(s)) for s in result.days[0].stops] == [("late", "2026-11-21 00:00")]
    days = days_plan(monkeypatch, pois, body)
    assert days.open_dates["late"] == [date(2026, 11, 20)]  # the night it is open on


def test_a_pinned_place_after_midnight_stays_on_its_own_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    late = {day: [["00:00", "04:00"]] for day in WEEK}
    pois = [
        poi("one", 0.001, "attraction.museum", hours=late),
        poi("two", 0.002, "attraction.museum", hours=late),
    ]
    pins = [{"id": "one", "date": "2026-11-21"}, {"id": "two", "date": "2026-11-20"}]
    body = timed(
        {"one": 60, "two": 60}, "2026-11-20T20:00", "2026-11-22T02:00",
        day_start="20:00", day_end="02:00", pins=pins,
    )  # fmt: skip
    result = plan(monkeypatch, pois, body)
    assert [[(s.poi.id, clock(s)) for s in day.stops] for day in result.days] == [
        [("two", "2026-11-21 00:00")],
        [("one", "2026-11-22 00:00")],
    ]


def test_overnight_days_keep_six_hours_of_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    evening = {day: [["18:00", "02:00"]] for day in WEEK}
    pois = [poi(f"l{i}", 0.002 * i, "attraction.landmark", hours=None) for i in range(1, 7)]
    pois += [poi("b0", 0.001, BAR, hours=evening), poi("b1", 0.013, BAR, hours=evening)]
    body = request(
        [p.id for p in pois], "2026-11-20T09:00", "2026-11-22T03:00",
        day_start="09:00", day_end="03:00",
    )  # fmt: skip
    days = days_plan(monkeypatch, pois, body)
    assert [day.capacity_minutes for day in days.days] == [18 * 60, 18 * 60]
    result = plan(monkeypatch, pois, body)
    assert not result.unscheduled and len(result.days) == 2
    for day in result.days:
        assert day.stops and day.stops[0].arrive.strftime("%H:%M") >= "09:00"
    first, second = result.days
    assert (second.stops[0].arrive - first.stops[-1].leave).total_seconds() >= 6 * H
    bars = [s for day in result.days for s in day.stops if s.poi.id.startswith("b")]
    assert len(bars) == 2 and all(s.start.hour >= 18 or s.start.hour < 2 for s in bars)
