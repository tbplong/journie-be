"""Group scored candidates into result sections (spec 6.7). Pure, no I/O.

* Every standard interest (or keyword routed to a group, e.g. "bar" -> nightlife) is a section
  of taxonomy nodes (see taxonomy.py and interest_map).
* A non-standard interest ("phở", "michelin") has a *home*: the taxonomy node holding at least
  ``HOME_SHARE`` of its hits, weighted by evidence. Description-only hits outside the home are
  dropped. It is merged into a requested section covering its home, which lifts its hits to the
  top with a reason; otherwise it becomes its own section of just its hits.
* A POI appears in one section only: the one where it scores highest; ties go to the more
  specific section (deeper matching node, then fewer nodes), then a required one, then the
  traveller's order. Sections are listed in the traveller's order.
"""

import math
from collections import Counter
from dataclasses import dataclass, field

from app.schemas.retrieval import PoiCard
from app.services.retrieval.config import INTEREST_MAP, Group
from app.services.retrieval.hours import HoursCheck, TripWindow, check_hours, decide
from app.services.retrieval.interests import AvoidRules, InterestPlan, InterestRequest
from app.services.retrieval.poi import Poi
from app.services.retrieval.scoring import (
    PREFERRED_BONUS,
    Evidence,
    Hit,
    card_flags,
    match_score,
    proximity,
    quality_score,
    reasons,
    time_fit,
    total,
)
from app.services.retrieval.taxonomy import covers, depth, top

PREFERRED_KEY = "preferred"
PREFERRED_LABEL = "Địa điểm bạn thích"
EMPTY_NOTE = "Chưa tìm thấy địa điểm phù hợp trong dữ liệu."
# Diversity caps per window of top items. Very common cuisines are not capped.
DIVERSITY_WINDOW = 8
DIVERSITY_DEPTH = 48
CAP_CUISINE, CAP_WARD, CAP_MICHELIN = 2, 4, 4
BROAD_CUISINES = frozenset({"vietnamese", "diner", "asian", "regional"})

# A keyword's home is the node with at least this share of its weighted hits. A hit found only
# in a description counts WEAK_WEIGHT; name and structured-column hits count 1. Starting values,
# tune with the eval set.
HOME_SHARE = 0.6
WEAK_WEIGHT = 0.3
WEAK_EVIDENCE = frozenset({"text"})

ANY_TIME = TripWindow(None, None, None)
KeywordHits = dict[str, dict[str, Evidence]]  # request key -> poi id -> evidence


@dataclass
class SectionSpec:
    key: str
    label: str
    required: bool
    order: int
    group: Group | None = None  # None: keyword-only section (its own request's hits)
    request: InterestRequest | None = None
    merged: list[InterestRequest] = field(default_factory=list)
    note: str | None = None

    def specificity(self, poi: Poi) -> tuple[float, int]:
        """Deeper matching node, then fewer nodes. A keyword-only section is the most specific:
        the traveller named exactly it."""
        if self.group is None:
            return (math.inf, 0)
        node = self.group.node_for(poi.category)
        return (depth(node) if node else 0, -len(self.group.nodes))

    @property
    def michelin_driven(self) -> bool:
        requests = [*self.merged, *([self.request] if self.request else [])]
        return any(r.keyword and "michelin" in r.keyword.filter for r in requests)


@dataclass
class Scored:
    poi: Poi
    section: str
    hits: list[Hit]
    check: HoursCheck
    flags: list[str]
    parts: dict[str, float]
    score: float
    preferred: bool

    @property
    def keyword_hit(self) -> bool:
        return any(hit.raw is not None for hit in self.hits)


@dataclass
class SectionResult:
    spec: SectionSpec
    items: list[Scored]


def accepts(group: Group, poi: Poi) -> bool:
    return group.node_for(poi.category) is not None


def avoided(poi: Poi, avoid: AvoidRules) -> bool:
    if any(covers(node, poi.category) for node in avoid.nodes):
        return True
    columns = {"michelin": (poi.michelin,), "cuisine": poi.cuisine, "features": poi.features}
    return any(
        set(columns[column]) & set(values)
        for keyword in avoid.filters
        for column, values in keyword.filter.items()
    )


def _label(request: InterestRequest) -> str:
    if request.keyword:
        return request.keyword.label
    return request.raw[:1].upper() + request.raw[1:]


def keyword_home(found: dict[str, Evidence], pois: dict[str, Poi]) -> str | None:
    """The taxonomy node holding >= HOME_SHARE of a keyword's weighted hits: the most specific
    category if one is dominant, else a top level, else None (hits are scattered)."""
    by_category: Counter[str] = Counter()
    for poi_id, evidence in found.items():
        poi = pois.get(poi_id)
        if poi is not None and poi.category:
            by_category[poi.category] += WEAK_WEIGHT if evidence in WEAK_EVIDENCE else 1.0
    weight = sum(by_category.values())
    by_top: Counter[str] = Counter()
    for category, value in by_category.items():
        by_top[top(category)] += value
    for counts in (by_category, by_top):
        if counts:
            node, value = max(counts.items(), key=lambda item: (item[1], item[0]))
            if value >= HOME_SHARE * weight:
                return node
    return None


def prune_hits(
    found: dict[str, Evidence], pois: dict[str, Poi]
) -> tuple[str | None, dict[str, Evidence]]:
    """(home, kept hits). A weak hit is kept only inside the home: a market whose description
    mentions phở is not a phở place. Without a home, only strong hits are kept if any exist."""
    found = {poi_id: evidence for poi_id, evidence in found.items() if poi_id in pois}
    home = keyword_home(found, pois)
    strong = {i: e for i, e in found.items() if e not in WEAK_EVIDENCE}
    if home is None:
        return None, strong or found
    weak = {
        i: e for i, e in found.items() if e in WEAK_EVIDENCE and covers(home, pois[i].category)
    }
    return home, {**strong, **weak}


def _host(groups: list[SectionSpec], home: str | None) -> SectionSpec | None:
    """The requested group section covering the keyword's home; the most specific one wins."""
    best: tuple[tuple[int, int, int], SectionSpec] | None = None
    for spec in groups:
        group = spec.group
        node = group.node_for(home) if group and home else None
        if group is None or node is None:
            continue
        rank = (depth(node), -len(group.nodes), -spec.order)
        if best is None or rank > best[0]:
            best = (rank, spec)
    return best[1] if best else None


def build_specs(
    plan: InterestPlan, hits: KeywordHits, homes: dict[str, str | None]
) -> tuple[list[SectionSpec], list[str]]:
    """Section specs in the traveller's order, plus warnings about requests that cannot be
    served. ``hits`` are already pruned (see prune_hits) and ``homes`` maps request keys to the
    keyword's home node."""
    warnings: list[str] = []
    requests = plan.requests or [
        InterestRequest(key, "prefer", "interest", group=key) for key in INTEREST_MAP.defaults
    ]
    groups: list[SectionSpec] = []
    for order, request in enumerate(requests):
        if request.group is None:
            continue
        group = INTEREST_MAP.groups[request.group]
        if not group.nodes:
            warnings.append(f"Không có dữ liệu cho “{request.raw}” tại TP.HCM")
        groups.append(
            SectionSpec(group.key, group.label, request.strength == "include", order, group, request)
        )

    specs = list(groups)
    for order, request in enumerate(requests):
        if request.group is not None:
            continue
        own = SectionSpec(
            request.key, _label(request), request.strength == "include", order, request=request
        )
        if not hits.get(request.key):
            # An `other` atom may not describe a place at all ("đi cùng trẻ em"): drop it
            # silently. A stated interest is warned about, and kept visible if required.
            if request.source == "interest":
                warnings.append(f"Không tìm thấy địa điểm cho “{request.raw}”")
                if request.strength == "include":
                    own.note = EMPTY_NOTE
                    specs.append(own)
            continue
        host = _host(groups, homes.get(request.key))
        if host is not None:
            host.merged.append(request)
            host.required = host.required or request.strength == "include"
        else:
            specs.append(own)
    specs.sort(key=lambda spec: spec.order)
    return specs, warnings


def _hits_for(
    spec: SectionSpec, poi: Poi, hits: KeywordHits
) -> list[Hit] | None:
    if spec.group is None:
        assert spec.request is not None
        evidence = hits.get(spec.request.key, {}).get(poi.id)
        if evidence is None:
            return None
        return [Hit(evidence, spec.request.strength, spec.request.raw)]
    if not accepts(spec.group, poi):
        return None
    found = [Hit("category", spec.request.strength if spec.request else "prefer")]
    for request in spec.merged:
        evidence = hits.get(request.key, {}).get(poi.id)
        if evidence is not None:
            found.append(Hit(evidence, request.strength, request.raw))
    return found


def _primary_cuisine(poi: Poi) -> str | None:
    return next((c for c in poi.cuisine if c not in BROAD_CUISINES), None)


def _diversity_keys(poi: Poi) -> list[str]:
    keys = []
    if cuisine := _primary_cuisine(poi):
        keys.append(f"c:{cuisine}")
    if poi.ward:
        keys.append(f"w:{poi.ward}")
    if poi.michelin:
        keys.append("m")
    return keys


def _fits(item: Scored, counts: Counter[str], michelin_capped: bool) -> bool:
    caps = {"c": CAP_CUISINE, "w": CAP_WARD, "m": CAP_MICHELIN if michelin_capped else None}
    for key in _diversity_keys(item.poi):
        cap = caps[key[0]]
        if cap is not None and counts[key] >= cap:
            return False
    return True


def diversify(items: list[Scored], michelin_capped: bool) -> list[Scored]:
    """Greedy re-rank of score-ordered items: per window of top items, cap repeats of
    cuisine/ward/Michelin. Past ``DIVERSITY_DEPTH`` the plain score order is kept.
    """
    remaining, tail = items[:DIVERSITY_DEPTH], items[DIVERSITY_DEPTH:]
    ordered: list[Scored] = []
    counts: Counter[str] = Counter()
    while remaining:
        if len(ordered) % DIVERSITY_WINDOW == 0:
            counts = Counter()
        pick = next((i for i in remaining if _fits(i, counts, michelin_capped)), remaining[0])
        remaining.remove(pick)
        ordered.append(pick)
        counts.update(_diversity_keys(pick.poi))
    return [*ordered, *tail]


def order_items(items: list[Scored], michelin_capped: bool) -> list[Scored]:
    """Preferred places first, then keyword hits merged into the section, then the rest;
    score order with diversity within each tier."""
    items = sorted(items, key=lambda s: (-s.score, s.poi.id))
    tiers = (
        [s for s in items if s.preferred],
        [s for s in items if not s.preferred and s.keyword_hit],
        [s for s in items if not s.preferred and not s.keyword_hit],
    )
    return [item for tier in tiers for item in diversify(tier, michelin_capped)]


@dataclass(frozen=True)
class _Context:
    hits: KeywordHits
    window: TripWindow
    anchors: list[tuple[float, float]]

    def hours(self, poi: Poi) -> tuple[HoursCheck, list[str]] | None:
        """Hours check and flags; None when trusted hours say the POI is closed."""
        active = self.window.active
        check = check_hours(poi.opening_hours, self.window if active else ANY_TIME)
        if not active:
            return check, []
        decision, flags = decide(check, poi.hours_confidence, poi.hours_contradicted)
        return None if decision == "exclude" else (check, flags)

    def score(
        self,
        poi: Poi,
        section: str,
        found: list[Hit],
        hours: tuple[HoursCheck, list[str]],
        preferred: bool,
    ) -> Scored:
        check, flags = hours
        parts: dict[str, float | None] = {
            "match": match_score(found),
            "quality": quality_score(poi),
            "time_fit": time_fit(check, flags) if self.window.active else None,
            "proximity": proximity(poi, self.anchors),
        }
        score = total(parts) + (PREFERRED_BONUS if preferred else 0.0)
        kept = {name: round(value, 3) for name, value in parts.items() if value is not None}
        if preferred:
            kept["preferred"] = PREFERRED_BONUS
        return Scored(poi, section, found, check, flags, kept, round(score, 4), preferred)


def _best_placement(
    poi: Poi, specs: list[SectionSpec], ctx: _Context, preferred: bool
) -> Scored | None:
    """Score a POI in every section that accepts it and keep the best one."""
    hours = ctx.hours(poi)
    if hours is None:
        return None
    best: tuple[tuple[float, tuple[float, int], bool, int], Scored] | None = None
    for spec in specs:
        found = _hits_for(spec, poi, ctx.hits)
        if found is None:
            continue
        scored = ctx.score(poi, spec.key, found, hours, preferred)
        rank = (round(scored.score, 6), spec.specificity(poi), spec.required, -spec.order)
        if best is None or rank > best[0]:
            best = (rank, scored)
    if best is None and preferred:
        return ctx.score(poi, PREFERRED_KEY, [Hit("name", "prefer")], hours, True)
    return best[1] if best else None


def _finish(spec: SectionSpec, items: list[Scored], warnings: list[str]) -> SectionResult | None:
    items = order_items(items, michelin_capped=not spec.michelin_driven)
    for request in spec.merged:
        if not any(hit.raw == request.raw for item in items for hit in item.hits):
            warnings.append(f"Không tìm thấy địa điểm cho “{request.raw}”")
    if items:
        return SectionResult(spec, items)
    if spec.required:
        spec.note = spec.note or EMPTY_NOTE
        return SectionResult(spec, items)
    if spec.group is None or spec.group.nodes:  # an empty group (beach) is already warned
        warnings.append(f"Không tìm thấy địa điểm cho “{_spec_raw(spec)}”")
    return None


def assemble(
    plan: InterestPlan,
    pois: dict[str, Poi],
    hits: KeywordHits,
    window: TripWindow,
    preferred_ids: frozenset[str] = frozenset(),
    pinned_ids: frozenset[str] = frozenset(),
    anchors: list[tuple[float, float]] | None = None,
) -> tuple[list[SectionResult], list[str]]:
    """Score every candidate, assign it to one section and order each section."""
    homes: dict[str, str | None] = {}
    pruned: KeywordHits = {}
    for key, found in hits.items():
        homes[key], pruned[key] = prune_hits(found, pois)
    specs, warnings = build_specs(plan, pruned, homes)
    ctx = _Context(pruned, window, anchors or [])
    members: dict[str, list[Scored]] = {spec.key: [] for spec in specs}
    for poi in pois.values():
        if poi.id in pinned_ids or avoided(poi, plan.avoid):
            continue
        scored = _best_placement(poi, specs, ctx, poi.id in preferred_ids)
        if scored is None:
            continue
        if scored.section not in members:
            last = max((spec.order for spec in specs), default=-1)
            specs.append(SectionSpec(PREFERRED_KEY, PREFERRED_LABEL, False, last + 1))
            members[PREFERRED_KEY] = []
        members[scored.section].append(scored)

    results = [_finish(spec, members[spec.key], warnings) for spec in specs]
    return [r for r in results if r is not None], list(dict.fromkeys(warnings))


def _spec_raw(spec: SectionSpec) -> str:
    return spec.request.raw if spec.request else spec.label


def to_card(item: Scored) -> PoiCard:
    poi = item.poi
    return PoiCard(
        id=poi.id,
        name=poi.name,
        kind=poi.kind,
        category=poi.category,
        ward=poi.ward,
        address=poi.address,
        lat=poi.latitude,
        lon=poi.longitude,
        score=item.score,
        score_parts=item.parts,
        reasons=reasons(poi, item.hits, item.check),
        flags=card_flags(poi, item.flags),
        price_raw=poi.price_raw,
        opening_hours_raw=poi.opening_hours_raw,
        website=poi.website,
        phone=poi.phone,
    )
