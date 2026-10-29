"""Turn an extraction's interests, ``other`` atoms and avoid list into retrieval requests.

Pure: the only outside input is the set of cuisine values present in the data, so an interest
such as "ramen" can filter ``cuisine`` even when the keyword map does not list it.
"""

from dataclasses import dataclass, field
from typing import Literal

from app.schemas.extraction import Extraction
from app.services.extraction.processing import NAME_ALIASES, place_key
from app.services.retrieval.config import INTEREST_MAP, KEYWORD_MAP, Keyword
from app.services.retrieval.text import norm

Strength = Literal["include", "prefer"]
Source = Literal["interest", "other"]


@dataclass(frozen=True)
class InterestRequest:
    """One thing the traveller asked for.

    Exactly one of: ``group`` (taxonomy-node section), ``keyword`` (structured column filter,
    optionally plus text search) or ``text`` (normalized full-text query).
    """

    raw: str
    strength: Strength
    source: Source
    group: str | None = None
    keyword: Keyword | None = None
    text: str | None = None

    @property
    def key(self) -> str:
        if self.group:
            return self.group
        if self.keyword:
            return f"kw:{self.keyword.key}"
        return f"kw:{(self.text or '').replace(' ', '_')}"


@dataclass(frozen=True)
class AvoidRules:
    nodes: frozenset[str] = frozenset()  # taxonomy nodes, e.g. entertainment.nightlife for "bar"
    filters: tuple[Keyword, ...] = ()
    names: tuple[str, ...] = ()  # left for name resolution


@dataclass
class InterestPlan:
    requests: list[InterestRequest] = field(default_factory=list)
    avoid: AvoidRules = field(default_factory=AvoidRules)


def _interest_group(value: str) -> str | None:
    """Canonical standard interest for a value, via the extraction alias table."""
    standard = NAME_ALIASES["interest"].get(place_key(value), value)
    return standard if standard in INTEREST_MAP.groups else None


def _keyword(value: str, cuisines: dict[str, str]) -> Keyword | None:
    key = norm(value)
    if key in KEYWORD_MAP.by_alias:
        return KEYWORD_MAP.by_alias[key]
    if key in cuisines:
        raw = cuisines[key]
        label = KEYWORD_MAP.cuisine_labels.get(raw, value)
        return Keyword(f"cuisine_{raw}", label, (key,), {"cuisine": (raw,)})
    return None


def request_for(
    value: str, strength: Strength, source: Source, cuisines: dict[str, str]
) -> InterestRequest | None:
    if group := _interest_group(value):
        return InterestRequest(value, strength, source, group=group)
    if keyword := _keyword(value, cuisines):
        if keyword.group:
            return InterestRequest(value, strength, source, group=keyword.group)
        return InterestRequest(value, strength, source, keyword=keyword)
    text = norm(value)
    return InterestRequest(value, strength, source, text=text) if text else None


def _strength(extraction: Extraction, field_name: str, value: str) -> Strength:
    hard = any(
        atom.field == field_name and atom.value == value and atom.operator in ("include", "eq")
        for atom in extraction.hard_constraints
    )
    return "include" if hard else "prefer"


def _avoid_rules(values: list[str], cuisines: dict[str, str]) -> AvoidRules:
    nodes: set[str] = set()
    filters: list[Keyword] = []
    names: list[str] = []
    for value in values:
        if group_key := _interest_group(value):
            nodes.update(INTEREST_MAP.groups[group_key].nodes)
        elif keyword := _keyword(value, cuisines):
            if keyword.group:
                nodes.update(INTEREST_MAP.groups[keyword.group].nodes)
            else:
                filters.append(keyword)
        else:
            names.append(value)
    return AvoidRules(frozenset(nodes), tuple(filters), tuple(names))


def plan_interests(extraction: Extraction, cuisines: dict[str, str] | None = None) -> InterestPlan:
    """Requests in the traveller's order, deduplicated by section key (include wins)."""
    cuisines = cuisines or {}
    candidates: list[InterestRequest] = []
    for value in extraction.interests:
        strength = _strength(extraction, "interests", value)
        request = request_for(value, strength, "interest", cuisines)
        if request:
            candidates.append(request)
    atom_lists: tuple[tuple[Strength, list], ...] = (
        ("include", extraction.hard_constraints),
        ("prefer", extraction.soft_constraints),
    )
    for atom_strength, atoms in atom_lists:
        for atom in atoms:
            if atom.field != "other" or atom.operator in ("exclude", "min", "max"):
                continue
            request = request_for(atom.value, atom_strength, "other", cuisines)
            if request:
                candidates.append(request)

    by_key: dict[str, InterestRequest] = {}
    for request in candidates:
        current = by_key.get(request.key)
        if current is None or (current.strength == "prefer" and request.strength == "include"):
            by_key[request.key] = request if current is None else _keep_first_raw(current, request)
    # `other` atoms that excluded something structured become avoid filters as well.
    other_avoid = [
        atom.value
        for atom in extraction.hard_constraints
        if atom.field == "other" and atom.operator == "exclude" and _keyword(atom.value, cuisines)
    ]
    avoid = _avoid_rules([*extraction.avoid, *other_avoid], cuisines)
    return InterestPlan(list(by_key.values()), avoid)


def _keep_first_raw(current: InterestRequest, stronger: InterestRequest) -> InterestRequest:
    return InterestRequest(
        current.raw, "include", current.source, stronger.group, stronger.keyword, stronger.text
    )
