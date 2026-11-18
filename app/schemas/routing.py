"""Itinerary routing request/response models (POST /routing)."""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.retrieval import PoiCard

# The solver's matrix is (places + 1)^2 and one OSRM table request carries every place; OSRM's
# default table limit is 100 coordinates.
MAX_PLACES = 50
MAX_DAYS = 14  # one tour per day; a longer trip is planned in parts
HHMM = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
TRIP_TZ = ZoneInfo("Asia/Ho_Chi_Minh")  # every place is in Ho Chi Minh City
TripDate = date  # a field named `date` with a default cannot use the bare `date` type


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
    day_end: str = Field(default="21:00", pattern=HHMM, description="End of every day.")
    pins: list[Pin] = Field(
        default_factory=list,
        max_length=MAX_PLACES,
        description="Places the traveller fixed to a day; the others are spread by the planner. "
        "Pin every place to keep the traveller's own split and only order each day.",
    )

    @field_validator("start_at", "end_at")
    @classmethod
    def trip_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=TRIP_TZ) if value.tzinfo is None else value.astimezone(TRIP_TZ)

    @model_validator(mode="after")
    def consistent(self) -> "RoutingRequest":
        ids = [place.id for place in self.places]
        if len(ids) != len(set(ids)):
            raise ValueError("places contains the same id twice")
        if unknown := set(self.must_visit_ids) - set(ids):
            raise ValueError(f"must_visit_ids not in places: {sorted(unknown)}")
        if self.day_end <= self.day_start:
            raise ValueError("day_end must be after day_start")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if (self.end_at.date() - self.start_at.date()).days >= MAX_DAYS:
            raise ValueError(f"the trip spans more than {MAX_DAYS} days")
        pinned = [pin.id for pin in self.pins]
        if len(pinned) != len(set(pinned)):
            raise ValueError("pins contains the same id twice")
        if unknown := set(pinned) - set(ids):
            raise ValueError(f"pins not in places: {sorted(unknown)}")
        first, last = self.start_at.date(), self.end_at.date()
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
    "`not_adjacent:<node>:<date>`: a travel rule (config/rules.yaml) the places did not allow "
    "keeping."
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
    capacity_minutes: int = Field(description="Length of the day (`day_start` to `day_end`).")


class DaysResponse(BaseModel):
    days: list[DayPlan]
    unscheduled: list[Unscheduled]
    open_dates: dict[str, list[date]] = Field(
        description="Per place id, the trip days it is open long enough for a visit: a place "
        "moved to another day is only valid on these."
    )
    warnings: list[str] = Field(description=WARNINGS)
    meta: RoutingMeta
