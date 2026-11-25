"""Nightlife rules: at most one place a day (day_max) and not before the evening (not_before).
Both soft: no place is dropped for them, a broken one is reported in `warnings`."""

import pytest

from app.services.routing import service
from app.services.routing.rules import RULES
from tests.test_routing_api import plan, poi, request
from tests.test_routing_days import TWO_DAYS, days_plan, ids, landmarks

BAR = "entertainment.nightlife"
EVENING = {day: [["18:00", "02:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
ONE_DAY = ("2026-11-20T08:00", "2026-11-20T21:00")


def bar(poi_id: str, dlat: float, hours=EVENING):
    return poi(poi_id, dlat, BAR, hours=hours)


def starts(result) -> dict[str, str]:
    return {s.poi.id: s.start.strftime("%H:%M") for day in result.days for s in day.stops}


def test_rules_file_has_the_nightlife_rules() -> None:
    assert RULES.day_max[BAR] == 1
    assert RULES.earliest(BAR) == (BAR, 18 * 3600)
    assert RULES.earliest("food.cafe") is None and RULES.earliest(None) is None


def test_not_before_moves_each_days_window() -> None:
    day = service.DAY_S
    slots = ((8 * 3600, 20 * 3600), (day + 8 * 3600, day + 17 * 3600))
    assert service.not_before(slots, 18 * 3600) == ((18 * 3600, 20 * 3600),)  # day 2 closes first


def test_one_bar_a_day_over_two_days(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [bar("b0", 0.001), bar("b1", 0.002), *landmarks("l", 6, dlat=0.003)]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS))
    per_day = [[place for place in day if place.startswith("b")] for day in ids(result)]
    assert sorted(map(len, per_day)) == [1, 1] and not result.unscheduled
    assert not [w for w in result.warnings if w.startswith(("day_max", "not_before"))]


def test_a_bar_open_all_day_is_visited_in_the_evening(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [bar("b0", 0.002, hours=None), *landmarks("l", 4, dlat=0.003)]
    result = plan(monkeypatch, pois, request([p.id for p in pois], *ONE_DAY))
    [day] = result.days
    assert starts(result)["b0"] >= "18:00"
    assert len(day.stops) == 5 and not [w for w in result.warnings if w.startswith("not_before")]


def test_two_bars_on_a_long_evening_are_kept_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [bar("b0", 0.001), bar("b1", 0.002)]
    body = request(["b0", "b1"], "2026-11-20T08:00", "2026-11-20T23:59", day_end="23:59")
    result = plan(monkeypatch, pois, body)
    assert sorted(starts(result)) == ["b0", "b1"] and min(starts(result).values()) >= "18:00"
    assert "day_max:entertainment.nightlife:2026-11-20" in result.warnings


def test_the_evening_gives_way_before_a_place_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Until 21:00 only one 90 min visit fits after 18:00: the second bar is visited earlier
    # rather than left out, and both broken rules are reported.
    all_day = {day: [["10:00", "02:00"]] for day in ("fri",)}
    pois = [bar("b0", 0.001, hours=all_day), bar("b1", 0.002, hours=all_day)]
    result = plan(monkeypatch, pois, request(["b0", "b1"], *ONE_DAY))
    assert sorted(starts(result)) == ["b0", "b1"] and not result.unscheduled
    assert {"day_max:entertainment.nightlife:2026-11-20", "not_before:entertainment.nightlife:2026-11-20"} <= set(result.warnings)


def test_a_bar_that_closes_before_the_evening_keeps_its_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    lunch_only = {"fri": [["10:00", "16:00"]]}
    result = plan(monkeypatch, [bar("b0", 0.002, hours=lunch_only)], request(["b0"], *ONE_DAY))
    assert starts(result) == {"b0": "10:00"}
    assert "not_before:entertainment.nightlife:2026-11-20" in result.warnings


def test_a_day_that_ends_before_the_evening_still_visits_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    body = request(["b0"], "2026-11-20T08:00", "2026-11-20T17:00", day_end="17:00")
    result = plan(monkeypatch, [bar("b0", 0.002, hours=None)], body)
    assert list(starts(result)) == ["b0"]
    assert "not_before:entertainment.nightlife:2026-11-20" in result.warnings


def test_pinning_two_bars_to_one_day_keeps_both(monkeypatch: pytest.MonkeyPatch) -> None:
    wide = {day: [["10:00", "02:00"]] for day in ("fri", "sat")}
    pois = [bar("b0", 0.001, hours=wide), bar("b1", 0.002, hours=wide), *landmarks("l", 2, dlat=0.003)]
    pins = [{"id": "b0", "date": "2026-11-20"}, {"id": "b1", "date": "2026-11-20"}]
    result = days_plan(monkeypatch, pois, request([p.id for p in pois], *TWO_DAYS, pins=pins))
    assert {"b0", "b1"} <= set(ids(result)[0]) and not result.unscheduled
    assert "day_max:entertainment.nightlife:2026-11-20" in result.warnings


def test_a_must_visit_late_bar_needs_a_later_day_end(monkeypatch: pytest.MonkeyPatch) -> None:
    late = {"fri": [["21:00", "05:00"]]}
    early_end = plan(monkeypatch, [bar("b0", 0.002, hours=late)], request(["b0"], *ONE_DAY, must_visit_ids=["b0"]))
    assert [(u.poi.id, u.reason) for u in early_end.unscheduled] == [("b0", "no_time")]
    body = request(["b0"], "2026-11-20T08:00", "2026-11-20T23:59", day_end="23:59", must_visit_ids=["b0"])
    assert starts(plan(monkeypatch, [bar("b0", 0.002, hours=late)], body)) == {"b0": "21:00"}
