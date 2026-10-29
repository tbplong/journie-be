"""Scores, reasons and flags for one POI in one section (spec 6.6). Pure.

``score_parts`` stay separate so ranking is explainable and the weights can be tuned with the
eval set. The total is a weighted mean over the parts that apply to the request: ``time_fit``
only when the traveller gave dates or times, ``proximity`` only when places are already chosen.
"""

import math
from dataclasses import dataclass
from typing import Literal

from app.services.retrieval.config import KEYWORD_MAP
from app.services.retrieval.hours import HoursCheck, hours_reason
from app.services.retrieval.poi import Poi

WEIGHTS = {"match": 0.55, "quality": 0.30, "time_fit": 0.15, "proximity": 0.15}
PREFERRED_BONUS = 0.25

# How well a POI matches a request, by the type of evidence.
MATCH = {"structured": 1.0, "name": 0.8, "category": 0.6, "text": 0.5}
STRENGTH = {"include": 1.0, "prefer": 0.85}
TIER = {"A": 1.0, "B": 0.6, "C": 0.3}
MICHELIN = {"star": 1.0, "bib_gourmand": 0.8, "selected": 0.5}
MICHELIN_LABELS = {
    "star": "Michelin 1 sao",
    "bib_gourmand": "Michelin Bib Gourmand",
    "selected": "Michelin Selected",
}
# Hours known but doubtful / unknown: below a confirmed open, above a likely closed.
TIME_FIT_UNKNOWN = 0.3
PROXIMITY_SCALE_KM = 2.0

Evidence = Literal["structured", "name", "category", "text"]


@dataclass(frozen=True)
class Hit:
    """Why a POI is a candidate for a section: evidence, request strength, user's word."""

    evidence: Evidence
    strength: Literal["include", "prefer"]
    raw: str | None = None  # set for keyword hits, shown in the reason


def match_score(hits: list[Hit]) -> float:
    return max((MATCH[hit.evidence] * STRENGTH[hit.strength] for hit in hits), default=0.0)


def quality_score(poi: Poi) -> float:
    # tier already folds in Michelin (A ~ Michelin or >=3 source families), so Michelin keeps
    # a small weight of its own and never dominates.
    tier = TIER.get(poi.tier or "", 0.0)
    recommended = min(poi.recommended_by_count or 0, 4) / 4
    michelin = MICHELIN.get(poi.michelin or "", 0.0)
    confidence = poi.confidence or 0.0
    return 0.45 * tier + 0.25 * recommended + 0.15 * michelin + 0.15 * confidence


def time_fit(check: HoursCheck | None, flags: list[str]) -> float | None:
    if check is None:
        return None
    if "hours_maybe_closed" in flags:
        return 0.0
    if check.status == "unknown":
        return TIME_FIT_UNKNOWN
    return check.fit


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def proximity(poi: Poi, anchors: list[tuple[float, float]]) -> float | None:
    if not anchors or poi.latitude is None or poi.longitude is None:
        return None
    nearest = min(haversine_km(poi.latitude, poi.longitude, lat, lon) for lat, lon in anchors)
    return math.exp(-nearest / PROXIMITY_SCALE_KM)


def total(parts: dict[str, float | None]) -> float:
    used = {name: value for name, value in parts.items() if value is not None}
    weight = sum(WEIGHTS[name] for name in used)
    return sum(WEIGHTS[name] * value for name, value in used.items()) / weight if weight else 0.0


def reasons(poi: Poi, hits: list[Hit], check: HoursCheck | None) -> list[str]:
    """Reasons built only from stored column values and the match evidence."""
    out: list[str] = []
    if poi.michelin in MICHELIN_LABELS:
        out.append(MICHELIN_LABELS[poi.michelin])
    for hit in hits:
        if hit.raw is None:
            continue
        if hit.evidence == "text":
            out.append(f"Mô tả có “{hit.raw}”")
        elif hit.evidence in ("name", "structured"):
            out.append(f"Khớp “{hit.raw}”")
    cuisine = next((c for c in poi.cuisine if c in KEYWORD_MAP.cuisine_labels), None)
    if cuisine:
        out.append(KEYWORD_MAP.cuisine_labels[cuisine])
    if {"vegetarian", "vegan"} & set(poi.features) and "Món chay" not in out:
        out.append("Có món chay")
    if check is not None and (text := hours_reason(poi.opening_hours, check.open_weekdays)):
        out.append(text)
    if (poi.recommended_by_count or 0) >= 2:
        out.append(f"Được {poi.recommended_by_count} nguồn giới thiệu")
    if poi.price_raw:
        out.append(f"Giá: {poi.price_raw}")
    return list(dict.fromkeys(out))


def card_flags(poi: Poi, hours_flags: list[str]) -> list[str]:
    flags = list(hours_flags)
    if not poi.price_raw:
        flags.append("no_price")
    return flags
