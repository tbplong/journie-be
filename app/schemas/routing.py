"""Itinerary routing request/response models (POST /routing)."""

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.retrieval import PoiCard

# The solver's matrix is (places + 1)^2 and one OSRM table request carries every place; OSRM's
# default table limit is 100 coordinates.
MAX_PLACES = 50
MAX_DAYS = 14  # one tour per day; a longer trip is planned in parts
HHMM = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
# A day_end at or before day_start ends the next day, at this hour at the latest. Every day
# uses the same window, so a trip of several days keeps the 6 h left of 24 h for rest.
LATEST_NEXT_DAY_END = "05:00"
MAX_DAY_HOURS = 20  # any trip day
MAX_DAY_HOURS_MULTI = 18  # the day window on a trip of more than one day
TRIP_TZ = ZoneInfo("Asia/Ho_Chi_Minh")  # every place is in Vietnam
TripDate = date  # a field named `date` with a default cannot use the bare `date` type


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Pin(BaseModel):
    id: str = Field(description="Id of one of `places`.")
    date: TripDate = Field(description="The trip day this place must be visited on.")


class RoutingRequest(BaseModel):
    places: list[PoiCard] = Field(
        min_length=1,
        max_length=MAX_PLACES,
        description="Places the traveller picked (cards from POST /retrieval), returned as sent. "
        "Only `id` is read: location, category and opening hours come from the database.",
    )
    must_visit_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_PLACES,
        description="Ids among `places` kept first when not every place fits.",
    )
    start: LatLon = Field(description="Where every day starts and ends (current location).")
    start_at: datetime = Field(
        description="Trip start; without a UTC offset it is read as Asia/Ho_Chi_Minh time."
    )
    end_at: datetime = Field(description="Trip end, same rule as `start_at`.")
    day_start: str = Field(default="08:00", pattern=HHMM, description="Start of days 2..n.")
    day_end: str = Field(
        default="21:00",
        pattern=HHMM,
        description="End of every day; at or before `day_start` it is the next day's, by "
        f"{LATEST_NEXT_DAY_END} at the latest (09:00-03:00 is an 18 h day).",
    )
    pins: list[Pin] = Field(
        default_factory=list,
        max_length=MAX_PLACES,
        description="Places the traveller fixed to a day; the others are spread by the planner. "
        "Pin every place to keep the traveller's own split and only order each day.",
    )

    @field_validator("start_at", "end_at")
    @classmethod
    def trip_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=TRIP_TZ)
        try:
            return value.astimezone(TRIP_TZ)
        except OverflowError:  # year 1 or 9999 shifted out of range; not a ValueError itself
            raise ValueError("date is out of range in Asia/Ho_Chi_Minh time") from None

    @property
    def overnight(self) -> bool:
        """Every day ends the next day: `day_end` at or before `day_start`."""
        return self.day_end <= self.day_start

    @property
    def day_close(self) -> time:
        """Until this time after midnight it is still the day before: `day_end` when days end
        the next day, else midnight itself."""
        return time.fromisoformat(self.day_end) if self.overnight else time()

    @property
    def last_day(self) -> date:
        """The last trip day: an end after midnight and by `day_close` closes the day before
        (exactly midnight, or 02:00 when days run until 03:00)."""
        last = self.end_at.date()
        if self.end_at.time() <= self.day_close and last > self.start_at.date():
            return last - timedelta(days=1)
        return last

    @model_validator(mode="after")
    def consistent(self) -> "RoutingRequest":
        ids = [place.id for place in self.places]
        if len(ids) != len(set(ids)):
            raise ValueError("places contains the same id twice")
        if unknown := set(self.must_visit_ids) - set(ids):
            raise ValueError(f"must_visit_ids not in places: {sorted(unknown)}")
        if self.overnight and self.day_end > LATEST_NEXT_DAY_END:
            raise ValueError(
                "day_end at or before day_start ends the next day, so it must be "
                f"{LATEST_NEXT_DAY_END} at the latest"
            )
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        days = (self.last_day - self.start_at.date()).days + 1
        if days > MAX_DAYS:
            raise ValueError(f"the trip spans more than {MAX_DAYS} days")
        end = _minutes(self.day_end) + (24 * 60 if self.overnight else 0)  # after day 1's midnight
        if days > 1 and end - _minutes(self.day_start) > MAX_DAY_HOURS_MULTI * 60:
            raise ValueError(
                f"the day from day_start to day_end lasts more than {MAX_DAY_HOURS_MULTI} h; "
                f"a trip of several days keeps {24 - MAX_DAY_HOURS_MULTI} h of rest between days"
            )
        midnight = self.start_at.replace(hour=0, minute=0, second=0, microsecond=0)
        first_end = timedelta(minutes=end)
        if days == 1:
            first_end = min(first_end, self.end_at - midnight)
        if first_end - (self.start_at - midnight) > timedelta(hours=MAX_DAY_HOURS):
            raise ValueError(
                f"the first day, from start_at to day_end, lasts more than {MAX_DAY_HOURS} h"
            )
        pinned = [pin.id for pin in self.pins]
        if len(pinned) != len(set(pinned)):
            raise ValueError("pins contains the same id twice")
        if unknown := set(pinned) - set(ids):
            raise ValueError(f"pins not in places: {sorted(unknown)}")
        first, last = self.start_at.date(), self.last_day
        if outside := sorted(pin.id for pin in self.pins if not first <= pin.date <= last):
            raise ValueError(f"pins outside the trip dates: {outside}")
        return self


class Leg(BaseModel):
    mode: Literal["walk", "ride"]
    distance_km: float
    minutes: int


class Stop(BaseModel):
    poi: PoiCard
    arrive: datetime
    start: datetime = Field(description="Start of the visit, after waiting for the opening.")
    leave: datetime
    wait_minutes: int
    visit_minutes: int
    leg: Leg = Field(description="Travel from the previous stop, or from `start` for the first.")


class Day(BaseModel):
    date: date
    stops: list[Stop]
    return_leg: Leg | None = Field(description="Back to `start`; null on a day with no stop.")


UnscheduledReason = Literal[
    "not_found", "no_location", "closed_on_trip_days", "closed_on_pinned_day", "no_time"
]


class Unscheduled(BaseModel):
    poi: PoiCard
    reason: UnscheduledReason = Field(
        description="`not_found`: id not in the database; `no_location`: no coordinates; "
        "`closed_on_trip_days`: no opening window long enough for the visit on any trip day; "
        "`closed_on_pinned_day`: the same, on the day it is pinned to; "
        "`no_time`: does not fit with the other places."
    )


class RoutingMeta(BaseModel):
    routing_version: str
    visit_minutes_version: str
    rules_version: str
    travel_source: Literal["osrm", "estimated"]
    latency_ms: int


WARNINGS = (
    "`travel_estimated`: some or all travel times are straight-line estimates (OSRM unset, "
    "unreachable or without a route); `hours_unknown:<id>`: no opening hours on record, "
    "assumed open all day; `day_max:<node>:<date>`, `day_spread:<node>`, "
    "`not_adjacent:<node>:<date>`, `not_before:<node>:<date>`: a travel rule (config/rules.yaml) "
    "the places did not allow keeping (`not_before`: a visit under the node, e.g. a bar, starts "
    "before its evening time)."
)


class RoutingResponse(BaseModel):
    days: list[Day]
    unscheduled: list[Unscheduled]
    warnings: list[str] = Field(description=WARNINGS)
    meta: RoutingMeta


class DayPlan(BaseModel):
    date: date
    places: list[PoiCard] = Field(description="In visiting order.")
    busy_minutes: int = Field(description="Visits plus travel, from and back to `start`.")
    capacity_minutes: int = Field(
        description="Length of the day: `day_start` to `day_end`, but from `start_at` on the "
        "first day and until `end_at` on the last."
    )


class DaysResponse(BaseModel):
    days: list[DayPlan]
    unscheduled: list[Unscheduled]
    open_dates: dict[str, list[date]] = Field(
        description="Per place id, the trip days it is open long enough for a visit: a place "
        "moved to another day is only valid on these."
    )
    warnings: list[str] = Field(description=WARNINGS)
    meta: RoutingMeta
