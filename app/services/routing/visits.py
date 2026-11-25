"""How long a visit to one place takes (config/visit_minutes.yaml).

One function for the whole backend: routing times stops with it and retrieval puts it on every
place card, so the frontend plans with the same number. In order: the place's own value
(`places`, researched per place), the restaurant style (`restaurant_style`), the category
(`categories`), then `default`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.services.retrieval.poi import Poi

VISIT_MINUTES_FILE = Path(__file__).with_name("config") / "visit_minutes.yaml"


def _words(text: str) -> str:
    """Lowercase words joined by single spaces and padded, so ' bún ' matches whole words only."""
    words = re.findall(r"\w+", unicodedata.normalize("NFC", text).lower())
    return f" {' '.join(words)} "


@dataclass(frozen=True)
class Style:
    minutes: int
    words: tuple[str, ...]
    cuisines: frozenset[str] = frozenset()

    def matches(self, name: str, cuisine: tuple[str, ...]) -> bool:
        if self.cuisines & set(cuisine):
            return True
        # Words keep their marks: "lầu" (a floor) is not "lẩu" (hot pot), and a name written
        # without marks ("Xin Chao", "Pho Co") is not read as cháo or phở; it keeps its category.
        text = _words(name)
        return any(f" {w} " in text for w in self.words)


@dataclass(frozen=True)
class VisitMinutes:
    version: str
    default: int
    by_category: dict[str, int]
    by_place: dict[str, int]
    michelin_star: int | None = None
    styles: tuple[Style, ...] = ()  # first match wins
    caps: dict[str, int] = field(default_factory=dict)  # top-level category or "default"

    def of(self, category: str | None) -> int:
        return self.by_category.get(category or "", self.default)

    def cap(self, category: str | None) -> int | None:
        """The longest visit a place of this category gets (`max_minutes`)."""
        return self.caps.get((category or "").split(".")[0], self.caps.get("default"))

    def of_place(self, poi: Poi) -> int:
        top = self.cap(poi.category)
        return min(self._of_place(poi), top) if top else self._of_place(poi)

    def _of_place(self, poi: Poi) -> int:
        if poi.id in self.by_place:
            return self.by_place[poi.id]
        if poi.category == "food.restaurant":
            if self.michelin_star and poi.michelin == "star":
                return self.michelin_star
            for style in self.styles:
                if style.matches(poi.name, poi.cuisine):
                    return style.minutes
        return self.of(poi.category)


def _style(entry: dict[str, Any]) -> Style:
    words = tuple(_words(w).strip() for w in entry["words"])
    return Style(entry["minutes"], words, frozenset(entry.get("cuisines") or ()))


def load_visit_minutes(path: Path = VISIT_MINUTES_FILE) -> VisitMinutes:
    """Loaded at import, so a bad edit fails at startup, not mid-request."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    style = data.get("restaurant_style") or {}
    caps = data.get("max_minutes") or {}
    table = VisitMinutes(
        version=data["version"],
        default=data["default"],
        by_category=dict(data["categories"]),
        by_place=dict(data.get("places") or {}),
        michelin_star=style.get("michelin_star"),
        styles=tuple(_style(style[key]) for key in ("quick", "long") if key in style),
        caps=dict(caps) if isinstance(caps, dict) else {"default": caps},
    )
    values = [table.default, *table.by_category.values(), *table.by_place.values()]
    values += [s.minutes for s in table.styles]
    values += [table.michelin_star] if table.michelin_star is not None else []
    if any(
        not isinstance(v, int) or isinstance(v, bool) or v <= 0
        for v in [*values, *table.caps.values()]
    ):
        raise ValueError(f"{path.name}: visit minutes must be positive integers")
    over = [c for c, v in table.by_category.items() if (top := table.cap(c)) and v > top]
    if over or ((top := table.caps.get("default")) and max(values) > top):
        raise ValueError(f"{path.name}: a visit is longer than max_minutes ({over or top})")
    if any(not s.words or not all(s.words) for s in table.styles):
        raise ValueError(f"{path.name}: each restaurant style needs non-empty words")
    return table


VISIT_MINUTES = load_visit_minutes()
