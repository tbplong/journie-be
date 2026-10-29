"""The in-memory POI record the pure retrieval steps work on (one ``pois`` row)."""

from dataclasses import dataclass, field
from typing import Any

from app.services.retrieval.hours import WeeklyHours

# Columns read by retrieval; candidates.py selects exactly these.
POI_COLUMNS = (
    "id",
    "name",
    "name_en",
    "kind",
    "category",
    "ward",
    "address",
    "latitude",
    "longitude",
    "cuisine",
    "features",
    "michelin",
    "tier",
    "recommended_by_count",
    "confidence",
    "opening_hours_raw",
    "opening_hours",
    "hours_confidence",
    "hours_contradicted",
    "price_raw",
    "website",
    "phone",
)


@dataclass(frozen=True)
class Poi:
    id: str
    name: str
    kind: str  # display only; sections use category
    category: str | None = None
    name_en: str | None = None
    ward: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cuisine: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    michelin: str | None = None
    tier: str | None = None
    recommended_by_count: int | None = None
    confidence: float | None = None
    opening_hours_raw: str | None = None
    opening_hours: WeeklyHours | None = field(default=None, hash=False, compare=False)
    hours_confidence: str | None = None
    hours_contradicted: bool | None = None
    price_raw: str | None = None
    website: str | None = None
    phone: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Poi":
        values = {name: row.get(name) for name in POI_COLUMNS}
        values["cuisine"] = tuple(values["cuisine"] or ())
        values["features"] = tuple(values["features"] or ())
        return cls(**values)
