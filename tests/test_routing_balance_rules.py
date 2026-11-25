"""Day-composition rules of rules-v5: museums, meal slots, the evening, outdoor hours, the
midday rest and the guards that keep a rule from giving way on a day that does not need it."""

import json
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from app.schemas.routing import RoutingRequest
from app.services.retrieval.poi import Poi
from app.services.routing import service
from app.services.routing.rules import RULES
from tests.test_routing_api import card, plan, poi, request
from tests.test_routing_days import TWO_DAYS, days_plan, ids, landmarks

DAY = "2026-11-20"
ONE_DAY = (f"{DAY}T08:00", f"{DAY}T21:00")
BAR = "entertainment.nightlife"
ALL_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OPEN_7_22 = {day: [["07:00", "22:00"]] for day in ALL_WEEK}


def pinned(pois, date=DAY):
    return [{"id": p.id, "date": date} for p in pois]


def starts(result) -> dict[str, str]:
    return {s.poi.id: s.start.strftime("%H:%M") for day in result.days for s in day.stops}


def order(result) -> list[str]:
    return [s.poi.id for s in result.days[0].stops]


def museums(prefix: str, count: int, dlat: float = 0.0):
    return [
        poi(f"{prefix}{i}", dlat + 0.002 * i, "attraction.museum", hours=None) for i in range(count)
    ]


def test_rules_file_has_the_museum_rules() -> None:
    assert RULES.day_max["attraction.museum"] == 2
    assert "attraction.museum" in RULES.day_spread
    assert (
        RULES.tag("attraction.museum") == "attraction.museum"
        and RULES.tag("attraction.gallery") == "attraction.gallery"
    )
    assert RULES.tag("food.cafe") == "food" and RULES.tag("attraction.landmark") == ""
    assert (
        RULES.is_meal("food.street_food")
        and RULES.is_meal("food.food_court")
        and not RULES.is_meal("food.cafe")
    )


def test_museums_are_spread_capped_and_never_back_to_back(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [*museums("m", 4), *landmarks("l", 6, dlat=0.01)]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    for day in ids(result):
        kinds = [place[0] for place in day]
        assert kinds.count("m") == 2
        assert all(not (a == b == "m") for a, b in pairwise(kinds))
    assert not result.unscheduled
    assert not [
        w for w in result.warnings if w.startswith(("day_max", "day_spread", "not_adjacent"))
    ]


def test_named_museums_are_kept_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = museums("m", 3)
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    assert sorted(order(result)) == ["m0", "m1", "m2"] and not result.unscheduled
    assert {f"day_max:attraction.museum:{DAY}", f"not_adjacent:attraction.museum:{DAY}"} <= set(
        result.warnings
    )


def test_two_pinned_meals_take_lunch_and_dinner(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [
        poi("r0", 0.001, hours=OPEN_7_22),
        poi("r1", 0.002, hours=OPEN_7_22),
        *landmarks("l", 2, dlat=0.003),
    ]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    at = starts(result)
    meals = sorted(at[p] for p in ("r0", "r1"))
    assert "11:00" <= meals[0] < "14:00" and "17:30" <= meals[1] < "20:30"
    assert not result.unscheduled


def test_three_pinned_meals_take_breakfast_lunch_and_dinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pois = [poi(f"r{k}", 0.001 * (k + 1), hours=OPEN_7_22) for k in range(3)] + landmarks(
        "l", 2, dlat=0.004
    )
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    meals = sorted(starts(result)[f"r{k}"] for k in range(3))
    assert (
        "06:30" <= meals[0] < "09:30"
        and "11:00" <= meals[1] < "14:00"
        and "17:30" <= meals[2] < "20:30"
    )


def test_a_day_that_starts_after_lunch_gives_its_meal_the_dinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pois = [poi("r0", 0.001, hours=OPEN_7_22), *landmarks("l", 2, dlat=0.003)]
    body = request(
        [p.id for p in pois],
        f"{DAY}T14:00",
        f"{DAY}T23:00",
        day_start="14:00",
        day_end="23:00",
        pins=pinned(pois),
    )
    result = plan(monkeypatch, pois, body)
    assert "17:30" <= starts(result)["r0"] < "20:30"


def test_a_meal_slot_gives_way_before_a_place_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The restaurant shuts at 15:00: it cannot have dinner hours, so the second meal stays a late lunch and nothing is left out.
    early = {day: [["07:00", "15:00"]] for day in ALL_WEEK}
    pois = [
        poi("r0", 0.001, hours=early),
        poi("r1", 0.002, hours=early),
        *landmarks("l", 2, dlat=0.003),
    ]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    assert not result.unscheduled and len(result.days[0].stops) == 4


def test_an_unpinned_split_keeps_todays_meal_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [
        poi("r0", 0.001, hours=OPEN_7_22),
        poi("r1", 0.002, hours=OPEN_7_22),
        *landmarks("l", 4, dlat=0.003),
    ]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    assert sorted(len([p for p in day if p.startswith("r")]) for day in ids(result)) == [1, 1]


def test_free_time_goes_after_lunch_and_a_sight_sits_between_the_meals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Request order puts both sights before lunch; one of them moves between lunch and dinner and is held for a rest.
    pois = [
        *landmarks("l", 2, dlat=0.003),
        poi("r0", 0.001, hours=OPEN_7_22),
        poi("r1", 0.002, hours=OPEN_7_22),
    ]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    stops = result.days[0].stops
    kinds = [s.poi.id[0] for s in stops]
    assert kinds == ["l", "r", "l", "r"]
    lunch, sight, dinner = stops[1], stops[2], stops[3]
    assert sight.wait_minutes >= 120  # the rest after lunch
    assert dinner.start.strftime("%H:%M") == "17:30" and sight.leave <= dinner.start
    assert max(s.wait_minutes for s in stops[1:]) <= 180 + 60
    assert lunch.wait_minutes == 0  # the morning started later instead of waiting for 11:00


def test_nothing_daytime_follows_the_evening(monkeypatch: pytest.MonkeyPatch) -> None:
    evening = {day: [["18:00", "02:00"]] for day in ALL_WEEK}
    pois = [
        poi("b0", 0.002, BAR, hours=evening),
        poi("r0", 0.001, hours=OPEN_7_22),
        poi("r1", 0.003, hours=OPEN_7_22),
        *landmarks("l", 3, dlat=0.004),
    ]
    body = request(
        [p.id for p in pois],
        f"{DAY}T10:00",
        f"{DAY}T23:30",
        day_start="10:00",
        day_end="23:30",
        pins=pinned(pois),
    )
    result = plan(monkeypatch, pois, body)
    names = order(result)
    assert names[-1] == "b0" and starts(result)["b0"] >= "18:00"
    dinner = max(("r0", "r1"), key=lambda r: starts(result)[r])
    assert (
        names.index(dinner) == len(names) - 2
    )  # dinner, then the bar: no sight after the evening began


def test_outdoor_sights_stay_out_of_the_midday(monkeypatch: pytest.MonkeyPatch) -> None:
    # The museums are nearest the start, so the shortest order sees them first and reaches the pagodas around noon.
    pagodas = [
        poi(f"p{k}", 0.006 + 0.002 * k, "culture_religion.pagoda", hours=None) for k in range(2)
    ]
    pois = [*museums("m", 2, dlat=0.001), poi("r0", 0.004, hours=OPEN_7_22), *pagodas]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    for p in pagodas:
        assert not "11:00" <= starts(result)[p.id] < "15:00"


def test_a_day_without_an_evening_rests_after_lunch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Open all day, so the day starts at 08:00 and the afternoon has room: the rest is cut short only where a
    # later visit would end after the evening begins (17:30) or its place would close.
    sights = [poi(f"l{k}", 0.003 + 0.002 * k, "attraction.landmark", hours=None) for k in range(3)]
    pois = [*sights, poi("r0", 0.001, hours=OPEN_7_22), *museums("m", 2, dlat=0.02)]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))
    stops = result.days[0].stops
    at = [s.poi.id for s in stops].index("r0")
    assert 0 < at < len(stops) - 1
    assert stops[at + 1].wait_minutes == 60  # six stops: an hour
    assert all(s.wait_minutes == 0 for s in stops[at + 2 :])


def test_the_rest_waits_out_the_midday_before_an_outdoor_sight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A quick lunch (35 minutes, the shop is open 12:00-13:00) then a pagoda that opens at noon:
    # the pagoda starts when the avoid hours end, not at lunch plus the rest for two stops.
    result = rest_day(monkeypatch, "12:00")
    assert order(result) == ["r0", "p0"]
    assert starts(result)["p0"] == "15:00"
    assert result.days[0].stops[1].wait_minutes <= RULES.rest_max // 60


def test_the_rest_never_runs_past_its_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lunch at 11:00: waiting until 15:00 would be over three hours, so the pagoda keeps the
    # plain rest and starts before the avoid hours end.
    result = rest_day(monkeypatch, "07:00")
    assert order(result) == ["r0", "p0"]
    assert starts(result)["p0"] < "15:00"
    assert result.days[0].stops[1].wait_minutes <= RULES.rest_max // 60


def rest_day(monkeypatch: pytest.MonkeyPatch, lunch_opens: str):
    shop = {day: [[lunch_opens, "13:00"]] for day in ALL_WEEK}
    lunch = replace(poi("r0", 0.001, hours=shop), name="Phở Lệ")
    noon = {day: [["12:00", "20:00"]] for day in ALL_WEEK}
    pois = [lunch, poi("p0", 0.003, "culture_religion.pagoda", hours=noon)]
    return plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY, pins=pinned(pois)))


def test_a_held_time_that_misses_a_window_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [
        *landmarks("l", 2, dlat=0.003),
        poi("r0", 0.001, hours=OPEN_7_22),
        poi("r1", 0.002, hours=OPEN_7_22),
    ]
    body = request([p.id for p in pois], *ONE_DAY, pins=pinned(pois))
    held = plan(monkeypatch, pois, body)
    monkeypatch.setattr(
        service, "holds", lambda prep, inst, d, tour: {0: 23 * 3600}
    )  # nothing can start at 23:00
    plain = plan(monkeypatch, pois, body)
    assert [s.poi.id for s in plain.days[0].stops] == [s.poi.id for s in held.days[0].stops]
    assert all(s.start.strftime("%H:%M") < "21:00" for s in plain.days[0].stops)


def test_only_the_day_that_needs_it_loses_the_evening_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    # Day 2 cannot hold both of its bars after 18:00; day 1 can hold its own and must.
    all_day = {day: [["10:00", "02:00"]] for day in ALL_WEEK}
    pois = [
        poi("b0", 0.001, BAR, hours=all_day),
        *landmarks("l", 2, dlat=0.002),
        poi("b1", 0.001, BAR, hours=all_day),
        poi("b2", 0.002, BAR, hours=all_day),
    ]
    pins = [*pinned(pois[:3], "2026-11-20"), *pinned(pois[3:], "2026-11-21")]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS, pins=pins))
    assert starts(result)["b0"] >= "18:00" and not result.unscheduled
    assert "not_before:entertainment.nightlife:2026-11-20" not in result.warnings
    assert "not_before:entertainment.nightlife:2026-11-21" in result.warnings


def test_a_real_three_day_trip_is_not_ordered_for_the_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the balance in the order step, day 2 of this trip came out as lunch, a 5 h 47 gap, the bar, dinner, then two
    # sights after 20:45: that longer order kept the day's busy share inside the tolerance.
    data = json.loads(
        (Path(__file__).parent / "fixtures" / "routing_pinned_three_days.json").read_text(
            encoding="utf-8"
        )
    )
    pois = [
        Poi(
            id=p["id"],
            name=p["id"],
            kind="x",
            category=p["category"],
            latitude=p["lat"],
            longitude=p["lon"],
            opening_hours=p["hours"],
        )
        for p in data["places"]
    ]
    keys = ("start", "start_at", "end_at", "day_start", "day_end", "pins", "must_visit_ids")
    body = RoutingRequest.model_validate(
        {"places": [card(p.id) for p in pois]} | {key: data[key] for key in keys}
    )
    result = plan(monkeypatch, pois, body)
    category = {p.id: p.category for p in pois}
    assert not result.unscheduled
    for day in result.days:
        kinds = [category[s.poi.id] for s in day.stops]
        if BAR in kinds:
            assert kinds.index(BAR) >= len(kinds) - 2 and all(
                k in (BAR, "food.restaurant") for k in kinds[kinds.index(BAR) :]
            )
            assert day.stops[kinds.index(BAR)].start.strftime("%H:%M") >= "18:00"
        assert max(s.wait_minutes for s in day.stops[1:]) <= 240


def test_the_day_balance_does_not_keep_a_longer_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every place is pinned, so no order can change the balance: each day gets its shortest order.
    near = landmarks("a", 4, dlat=0.001)
    far = [
        poi(f"z{k}", 0.03 * (1 if k % 2 else -1) * (k + 1), "attraction.landmark") for k in range(2)
    ]
    pins = [*pinned(near, "2026-11-20"), *pinned(far, "2026-11-21")]
    result = plan(
        monkeypatch, [*near, *far], request([p.id for p in [*near, *far]], *TWO_DAYS, pins=pins)
    )
    first = [s.poi.id for s in result.days[0].stops]
    assert first in (["a0", "a1", "a2", "a3"], ["a3", "a2", "a1", "a0"])


def pagodas(prefix: str, count: int, dlat: float = 0.0):
    return [
        poi(f"{prefix}{i}", dlat + 0.002 * i, "culture_religion.pagoda", hours=None)
        for i in range(count)
    ]


def test_pagodas_are_one_a_day_when_the_days_have_room(monkeypatch: pytest.MonkeyPatch) -> None:
    # At most two a day (the fast pace's), and spread: two pagodas over two days are one each
    assert RULES.day_max["culture_religion"] == 2 and "culture_religion" in RULES.day_spread
    pois = [*pagodas("p", 2), *landmarks("l", 6, dlat=0.01)]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    per_day = [sum(place.startswith("p") for place in day) for day in ids(result)]
    assert per_day == [1, 1]
    assert not [w for w in result.warnings if "culture_religion" in w]


def test_many_pinned_pagodas_are_shared_evenly(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [*pagodas("p", 4), *landmarks("l", 2, dlat=0.01)]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    per_day = sorted(sum(place.startswith("p") for place in day) for day in ids(result))
    assert per_day == [2, 2]
