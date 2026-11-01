"""Opening-hours checks against the traveller's free window (spec 6.5). Pure, no I/O.

Hours are mapped onto a circular week of minutes (Mon 00:00 = 0). An interval whose close is
before its open runs past midnight into the next day; Sunday wraps to Monday. A place counts as
open on a trip day if any opening interval overlaps that day's window (it need not cover it).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = ("thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật")
DAY_MIN = 24 * 60
WEEK_MIN = 7 * DAY_MIN

WeeklyHours = dict[str, list[list[str]]]
Interval = tuple[int, int]  # half-open [start, end) in week minutes, 0 <= start < end <= WEEK_MIN
HoursStatus = Literal["open", "closed", "unknown"]


def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value <= DAY_MIN:
        raise ValueError(f"Bad time {hhmm!r}")
    return value


def _wrap(start: int, end: int) -> list[Interval]:
    """Split an interval that runs past Sunday midnight into two in-week pieces."""
    start, end = start % WEEK_MIN, start % WEEK_MIN + (end - start)
    if end <= WEEK_MIN:
        return [(start, end)]
    return [(start, WEEK_MIN), (0, end - WEEK_MIN)]


def weekly_intervals(hours: WeeklyHours | None) -> list[Interval] | None:
    """Week-minute intervals, or None when the record has no usable hours at all."""
    if not hours or not any(hours.get(day) for day in DAYS):
        return None
    intervals: list[Interval] = []
    for index, day in enumerate(DAYS):
        for open_at, close_at in hours.get(day) or []:
            start, end = to_minutes(open_at), to_minutes(close_at)
            if end <= start:  # past midnight; equal open/close means round the clock
                end += DAY_MIN
            intervals.extend(_wrap(index * DAY_MIN + start, index * DAY_MIN + end))
    return intervals


@dataclass(frozen=True)
class TripWindow:
    """When the traveller is free: weekdays (0 = Mon; None = any day) and a daily clock window.

    ``active`` is False when the extraction says nothing about dates or times, so no hours
    filtering applies.
    """

    weekdays: tuple[int, ...] | None
    start: int | None
    end: int | None

    @property
    def active(self) -> bool:
        return self.weekdays is not None or self.start is not None or self.end is not None

    def day_window(self, weekday: int) -> list[Interval]:
        start = 0 if self.start is None else self.start
        end = DAY_MIN if self.end is None else self.end
        if self.start is not None and self.end is not None and end <= start:
            end += DAY_MIN  # 22:00-02:00 runs into the next morning
        if end == start:
            end = start + 1
        return _wrap(weekday * DAY_MIN + start, weekday * DAY_MIN + end)


def trip_weekdays(
    start_date: str | None, end_date: str | None, duration_days: int | None
) -> tuple[int, ...] | None:
    """Distinct weekdays the trip covers, in trip order; None when no date is known."""
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    try:
        if start and not end and duration_days:
            end = start + timedelta(days=duration_days - 1)
        if end and not start and duration_days:
            start = end - timedelta(days=duration_days - 1)
    except OverflowError:  # a trip ending past year 9999 is not a real trip: no dates
        return None
    start, end = start or end, end or start
    if start is None or end is None or end < start:
        return None
    days = min((end - start).days + 1, 7)
    weekdays = ((start + timedelta(days=offset)).weekday() for offset in range(days))
    return tuple(dict.fromkeys(weekdays))


def trip_window(
    start_date: str | None,
    end_date: str | None,
    duration_days: int | None,
    available_start: str | None,
    available_end: str | None,
) -> TripWindow:
    return TripWindow(
        trip_weekdays(start_date, end_date, duration_days),
        None if available_start is None else to_minutes(available_start),
        None if available_end is None else to_minutes(available_end),
    )


@dataclass(frozen=True)
class HoursCheck:
    status: HoursStatus
    open_weekdays: tuple[int, ...]  # weekdays (of those checked) with an overlap
    fit: float  # share of checked weekdays that are open, 0..1


def _overlaps(first: list[Interval], second: list[Interval]) -> bool:
    return any(a < d and c < b for a, b in first for c, d in second)


def check_hours(hours: WeeklyHours | None, window: TripWindow) -> HoursCheck:
    intervals = weekly_intervals(hours)
    if intervals is None:
        return HoursCheck("unknown", (), 0.0)
    weekdays = window.weekdays if window.weekdays is not None else tuple(range(7))
    open_days = tuple(day for day in weekdays if _overlaps(intervals, window.day_window(day)))
    return HoursCheck("open" if open_days else "closed", open_days, len(open_days) / len(weekdays))


HoursDecision = Literal["keep", "exclude"]


def decide(
    check: HoursCheck, confidence: str | None, contradicted: bool | None
) -> tuple[HoursDecision, list[str]]:
    """Apply the confidence rules: only a trusted 'closed' excludes; doubtful ones are flagged."""
    flags: list[str] = []
    if contradicted:
        flags.append("hours_contradicted")
    if check.status == "unknown":
        return "keep", [*flags, "hours_unknown"]
    trusted = confidence in ("medium", "high") and not contradicted
    if check.status == "closed":
        return ("exclude", flags) if trusted else ("keep", [*flags, "hours_maybe_closed"])
    if confidence not in ("medium", "high"):
        flags.append("hours_low_confidence")
    return "keep", flags


def _format_day(hours: WeeklyHours, day: int) -> str:
    return ", ".join(f"{a}–{b}" for a, b in hours.get(DAYS[day]) or [])


def hours_reason(hours: WeeklyHours | None, open_weekdays: tuple[int, ...]) -> str | None:
    """A reason built only from stored hours, e.g. 'Mở 06:00–22:00 thứ Bảy'."""
    if not hours or not open_weekdays:
        return None
    if all(hours.get(day) == [["00:00", "24:00"]] for day in DAYS):
        return "Mở cửa 24/7"
    texts = {day: _format_day(hours, day) for day in open_weekdays}
    if len(open_weekdays) == 7 and len(set(texts.values())) == 1:
        return f"Mở {texts[0]} hằng ngày"
    first = open_weekdays[0]
    return f"Mở {texts[first]} {DAY_LABELS[first]}"
