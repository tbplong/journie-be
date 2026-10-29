from app.services.retrieval.hours import (
    DAYS,
    TripWindow,
    check_hours,
    decide,
    hours_reason,
    to_minutes,
    trip_weekdays,
    trip_window,
    weekly_intervals,
)

MON, TUE, SAT, SUN = 0, 1, 5, 6


def every_day(*intervals: tuple[str, str]) -> dict[str, list[list[str]]]:
    return {day: [list(pair) for pair in intervals] for day in DAYS}


def window(start: str | None, end: str | None, *weekdays: int) -> TripWindow:
    return TripWindow(
        weekdays or None,
        None if start is None else to_minutes(start),
        None if end is None else to_minutes(end),
    )


def test_trip_weekdays_from_dates_duration_or_nothing() -> None:
    # 2026-10-31 is a Saturday.
    assert trip_weekdays("2026-10-31", "2026-11-01", None) == (SAT, SUN)
    assert trip_weekdays("2026-10-31", None, 3) == (SAT, SUN, MON)
    assert trip_weekdays(None, "2026-11-01", 2) == (SAT, SUN)
    assert trip_weekdays("2026-10-31", None, None) == (SAT,)
    assert trip_weekdays(None, None, 3) is None
    assert len(trip_weekdays("2026-10-01", "2026-10-20", None) or ()) == 7


def test_no_dates_or_times_means_no_filter() -> None:
    assert not trip_window(None, None, 2, None, None).active
    assert trip_window(None, None, None, "08:00", None).active


def test_overlap_is_enough_and_lunch_break_splits_the_day() -> None:
    lunch_break = every_day(("07:00", "11:00"), ("14:00", "18:00"))
    assert check_hours(lunch_break, window("10:30", "12:00")).status == "open"
    assert check_hours(lunch_break, window("11:30", "13:30")).status == "closed"
    assert check_hours(lunch_break, window("17:59", "20:00")).status == "open"
    # Half-open: closing exactly when the window starts is not an overlap.
    assert check_hours(lunch_break, window("18:00", "20:00")).status == "closed"


def test_venue_open_past_midnight_covers_early_morning_next_day() -> None:
    hours = {day: [] for day in DAYS} | {"sat": [["20:00", "02:00"]]}
    assert check_hours(hours, window("01:00", "03:00", SUN)).status == "open"
    assert check_hours(hours, window("01:00", "03:00", SAT)).status == "closed"


def test_sunday_night_wraps_into_monday() -> None:
    hours = {day: [] for day in DAYS} | {"sun": [["22:00", "03:00"]]}
    assert check_hours(hours, window("01:00", "02:00", MON)).status == "open"


def test_overnight_window_reaches_into_next_day() -> None:
    breakfast = {day: [] for day in DAYS} | {"sun": [["06:00", "10:00"]]}
    # Saturday 22:00 -> Sunday 07:00 overlaps Sunday breakfast.
    assert check_hours(breakfast, window("22:00", "07:00", SAT)).status == "open"
    assert check_hours(breakfast, window("22:00", "05:00", SAT)).status == "closed"


def test_round_the_clock_and_closed_days() -> None:
    always = every_day(("00:00", "24:00"))
    assert check_hours(always, window("03:00", "04:00", TUE)).status == "open"
    closed_monday = every_day(("09:00", "17:00")) | {"mon": []}
    check = check_hours(closed_monday, window(None, None, MON, TUE))
    assert check.status == "open"
    assert check.open_weekdays == (TUE,)
    assert check.fit == 0.5
    assert check_hours(closed_monday, window(None, None, MON)).status == "closed"


def test_single_endpoint_uses_the_rest_of_the_day() -> None:
    evening = every_day(("18:00", "22:00"))
    assert check_hours(evening, window("20:00", None)).status == "open"
    assert check_hours(evening, window(None, "17:00")).status == "closed"
    assert check_hours(evening, window(None, "18:30")).status == "open"


def test_missing_hours_are_unknown() -> None:
    assert weekly_intervals(None) is None
    assert weekly_intervals({day: [] for day in DAYS}) is None
    assert check_hours(None, window("08:00", "10:00")).status == "unknown"


def test_confidence_rules() -> None:
    closed = check_hours(every_day(("18:00", "22:00")), window("08:00", "10:00"))
    opened = check_hours(every_day(("08:00", "22:00")), window("08:00", "10:00"))
    assert decide(closed, "high", False) == ("exclude", [])
    assert decide(closed, "medium", False) == ("exclude", [])
    assert decide(closed, "low", False) == ("keep", ["hours_maybe_closed"])
    assert decide(closed, "high", True) == ("keep", ["hours_contradicted", "hours_maybe_closed"])
    assert decide(opened, "low", False) == ("keep", ["hours_low_confidence"])
    assert decide(opened, "high", False) == ("keep", [])


def test_hours_reason_uses_stored_values() -> None:
    assert hours_reason(every_day(("00:00", "24:00")), (SAT,)) == "Mở cửa 24/7"
    assert hours_reason(every_day(("06:00", "22:00")), tuple(range(7))) == (
        "Mở 06:00–22:00 hằng ngày"
    )
    assert hours_reason(every_day(("06:00", "22:00")), (SAT,)) == "Mở 06:00–22:00 thứ Bảy"
    assert hours_reason(None, (SAT,)) is None
