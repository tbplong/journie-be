"""Group scored candidates into result sections (spec 6.7). Pure, no I/O.

* Every standard interest (or keyword routed to a group, e.g. "bar" -> nightlife) is a section
  of taxonomy nodes (see taxonomy.py and interest_map).
* A non-standard interest ("phở", "michelin") has a *home*: the taxonomy node holding at least
  ``HOME_SHARE`` of its hits, weighted by evidence. Description-only hits outside the home are
  dropped. It is merged into a requested section covering its home, which lifts its hits to the
  top with a reason; otherwise it becomes its own section of just its hits.
* A text request that comes with a group (InterestRequest.companion: "bia", "kiếm chỗ uống
  rượu" -> nightlife) is always merged into that group's section, whatever its home: its hits
  inside the group are lifted there, so the section never loses its places to a section named
  after the words. Outside the group, only places holding the traveller's words (a word hit,
  not meaning alone) can make a section, and only when they have a home (Hà Nội "bia" -> the
  "Bia hơi …" restaurants); that section never takes the group's places.
* A POI appears in one section only: the most specific one accepting it (deeper matching node,
  then fewer nodes; a keyword-only section is the most specific), then the one where it scores
  highest, then a required one, then the traveller's order. Sections are listed in the
  traveller's order.
"""

import math
from collections import Counter
from collections.abc import Callable
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
    explore,
    match_score,
    proximity,
    quality_score,
    reasons,
    time_fit,
    total,
)
from app.services.retrieval.taxonomy import covers, depth, top
from app.services.routing.visits import VISIT_MINUTES

PREFERRED_KEY = "preferred"
PREFERRED_LABEL = "Địa điểm bạn thích"
EMPTY_NOTE = "Chưa tìm thấy địa điểm phù hợp trong dữ liệu."
# Diversity caps per window of top items. Very common cuisines are not capped. "Famous" is
# tier A or Michelin: capped so well-documented places don't fill every list.
DIVERSITY_WINDOW = 8
DIVERSITY_DEPTH = 48
CAP_CUISINE, CAP_WARD, CAP_FAMOUS = 2, 4, 3
BROAD_CUISINES = frozenset({"vietnamese", "diner", "asian", "regional"})

# A keyword's home is the node with at least this share of its weighted hits. A hit found only
# in a description counts WEAK_WEIGHT; name and structured-column hits count 1. Re-checked on
# the 2026-11-24 eval after re-embedding (92 cases, SIMILARITY_MIN 0.65): 0.45-0.55 give the
# scattered hits of "chỗ cho trẻ em" a food home, which drops Đầm Sen and the malls; 0.65 and
# 0.7 change no case. WEAK_WEIGHT 0.2-0.5 changes no case. (Before companions stayed in their
# group's section, the value also decided which drinking texts kept the bars.)
HOME_SHARE = 0.6
WEAK_WEIGHT = 0.3
WEAK_EVIDENCE = frozenset({"text", "semantic"})
# Semantic hits kept per request: the closest places that can be suggested (open, not avoided,
# not already picked). semantic.py over-fetches so these filters do not shrink the list. On the
# same eval 15-20 drop Đầm Sen from "chỗ cho trẻ em"; 40 and 60 change no case.
SEMANTIC_KEEP = 30
# Without a home, weak hits are dropped only when at least this many strong hits carry the
# keyword: one stray name hit ("rượu" -> a restaurant named Hầm Rượu) must not discard a dozen
# description hits.
STRONG_HITS_MIN = 3
NO_MATCH_DEFAULTS = "Chưa tìm thấy địa điểm khớp “{}”; đang gợi ý chung"

ANY_TIME = TripWindow(None, None, None)
KeywordHits = dict[str, dict[str, Evidence]]  # request key -> poi id -> evidence
Closeness = dict[str, dict[str, float]]  # request key -> poi id -> semantic closeness (0..1)


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
    # A companion's own section (group None): only places this group does not accept.
    outside: Group | None = None

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


def _home_sample(found: dict[str, Evidence], close: dict[str, float]) -> dict[str, Evidence]:
    """Word hits plus the SEMANTIC_KEEP closest semantic hits, already picked places included:
    the home does not move while the traveller picks places from the section."""
    semantic = [i for i, e in found.items() if e == "semantic"]
    keep = set(sorted(semantic, key=lambda i: (-close.get(i, 0.0), i))[:SEMANTIC_KEEP])
    return {i: e for i, e in found.items() if e != "semantic" or i in keep}


def prune_hits(
    found: dict[str, Evidence], pois: dict[str, Poi], close: dict[str, float] | None = None
) -> tuple[str | None, dict[str, Evidence]]:
    """(home, kept hits). A weak hit is kept only inside the home: a market whose description
    mentions phở is not a phở place. Without a home, only strong hits are kept if there are at
    least STRONG_HITS_MIN of them. The home is computed on _home_sample (``close`` grades the
    semantic hits), the pruning applies to every hit."""
    found = {poi_id: evidence for poi_id, evidence in found.items() if poi_id in pois}
    home = keyword_home(_home_sample(found, close or {}), pois)
    strong = {i: e for i, e in found.items() if e not in WEAK_EVIDENCE}
    if home is None:
        return None, strong if len(strong) >= STRONG_HITS_MIN else found
    weak = {
        i: e for i, e in found.items() if e in WEAK_EVIDENCE and covers(home, pois[i].category)
    }
    return home, {**strong, **weak}


def companion_hits(
    found: dict[str, Evidence], pois: dict[str, Poi], group: Group
) -> tuple[str | None, dict[str, Evidence]]:
    """(home of the hits outside ``group``, kept hits) of a companion request (see the module
    docstring). Every hit inside the group is kept. Outside it, the home is taken on the word
    hits only (keyword_home); weak hits outside that home are dropped as in prune_hits. Without
    a word hit or a home, every outside hit is dropped: cafés close in meaning to "kiếm chỗ
    uống rượu" are not an answer, the group is."""
    found = {i: e for i, e in found.items() if i in pois}
    inside = {i: e for i, e in found.items() if accepts(group, pois[i])}
    outside = {i: e for i, e in found.items() if i not in inside}
    home = keyword_home({i: e for i, e in outside.items() if e != "semantic"}, pois)
    if home is None:
        return None, inside
    kept = {
        i: e for i, e in outside.items() if e not in WEAK_EVIDENCE or covers(home, pois[i].category)
    }
    return home, {**inside, **kept}


def _closest_semantic(
    found: dict[str, Evidence], close: dict[str, float], usable: Callable[[str], bool]
) -> dict[str, Evidence]:
    """Word hits plus the SEMANTIC_KEEP closest usable semantic-only hits."""
    semantic = [i for i, e in found.items() if e == "semantic" and usable(i)]
    keep = set(sorted(semantic, key=lambda i: (-close.get(i, 0.0), i))[:SEMANTIC_KEEP])
    return {i: e for i, e in found.items() if e != "semantic" or i in keep}


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


def _merge(host: SectionSpec, request: InterestRequest) -> None:
    host.merged.append(request)
    host.required = host.required or request.strength == "include"


def may_use_defaults(requests: list[InterestRequest]) -> bool:
    """Only `other` wishes outside every group (or nothing) were asked: the default sections are
    shown unless one of them has a place left to show (uses_defaults). service.retrieve fetches
    the default sections' places whenever this holds, so the decision is taken once, in
    build_specs."""
    return not any(r.source == "interest" or r.group for r in requests)


def uses_defaults(requests: list[InterestRequest], left: frozenset[str]) -> bool:
    """``left``: keys of the requests with a place still to show (see build_specs)."""
    return may_use_defaults(requests) and not any(r.key in left for r in requests)


def build_specs(
    plan: InterestPlan,
    hits: KeywordHits,
    homes: dict[str, str | None],
    found: frozenset[str] | None = None,
    unsearched: frozenset[str] = frozenset(),
    left: frozenset[str] | None = None,
) -> tuple[list[SectionSpec], list[str]]:
    """Section specs in the traveller's order, plus warnings about requests that cannot be
    served. ``hits`` are already pruned (see prune_hits) and ``homes`` maps request keys to the
    keyword's home node. ``found``: keys of the requests whose search found a place that can be
    suggested (open, not avoided), picked or not. ``left``: keys of the requests with such a
    place not picked yet; the default sections replace `other` wishes with none left. Both
    default to the keys of ``hits``. ``unsearched``: keys of free-text requests the semantic
    cap left to word matching (the service warns about them), so they are not reported as not
    found."""
    # Imported here: semantic imports this module.
    from app.services.retrieval.semantic import negated

    warnings: list[str] = []
    requests = plan.requests
    with_hits = frozenset(k for k, v in hits.items() if v)
    found = with_hits if found is None else found
    left = with_hits if left is None else left
    # Defaults unless something can fill a section: an `other` atom that finds no place may not
    # describe a place at all ("đi cùng trẻ em"), so it is named in a warning, not as an empty
    # section. A refusal ("không uống rượu") is not a wish for a place. A wish whose places
    # are all picked found them: no warning, and no 'not found' for its empty section either.
    if uses_defaults(requests, left):
        for request in requests:
            if request.key not in found | unsearched and not negated(request.raw):
                warnings.append(NO_MATCH_DEFAULTS.format(request.raw))
        requests = [
            InterestRequest(key, "prefer", "interest", group=key) for key in INTEREST_MAP.defaults
        ]
    groups: list[SectionSpec] = []
    for order, request in enumerate(requests):
        if request.group is None:
            continue
        group = INTEREST_MAP.groups[request.group]
        # A group without data whose companion text found places ("ăn hải sản ven biển") is
        # answered by those places, not by a no-data warning.
        answered = any(r.companion == group.key and r.key in found for r in requests)
        if not group.nodes and not answered:
            warnings.append(f"Không có dữ liệu cho “{request.raw}” ở thành phố này")
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
            if request.key in unsearched:
                continue
            if request.source == "interest":
                warnings.append(f"Không tìm thấy địa điểm cho “{request.raw}”")
                if request.strength == "include":
                    own.note = EMPTY_NOTE
                    specs.append(own)
            continue
        home = homes.get(request.key)
        companion = next(
            (s for s in groups if s.group and s.group.nodes and s.group.key == request.companion),
            None,
        )
        if companion is not None:
            # Its hits inside the group are lifted there; the rest go to a host covering their
            # home, else to a section of their own that never takes the group's places.
            _merge(companion, request)
            host = _host([s for s in groups if s is not companion], home)
            if host is not None:
                _merge(host, request)
            elif home is not None:
                own.outside = companion.group
                specs.append(own)
            continue
        host = _host(groups, home)
        if host is not None:
            _merge(host, request)
        else:
            specs.append(own)
    specs.sort(key=lambda spec: spec.order)
    return specs, warnings


def _hits_for(
    spec: SectionSpec, poi: Poi, hits: KeywordHits, closeness: Closeness
) -> list[Hit] | None:
    def hit(request: InterestRequest) -> Hit | None:
        evidence = hits.get(request.key, {}).get(poi.id)
        if evidence is None:
            return None
        close = closeness.get(request.key, {}).get(poi.id, 1.0)
        return Hit(evidence, request.strength, request.raw, close)

    if spec.group is None:
        assert spec.request is not None
        if spec.outside is not None and accepts(spec.outside, poi):
            return None
        found = hit(spec.request)
        return None if found is None else [found]
    if not accepts(spec.group, poi):
        return None
    return [
        Hit("category", spec.request.strength if spec.request else "prefer"),
        *filter(None, map(hit, spec.merged)),
    ]


def _primary_cuisine(poi: Poi) -> str | None:
    return next((c for c in poi.cuisine if c not in BROAD_CUISINES), None)


def _diversity_keys(poi: Poi) -> list[str]:
    keys = []
    if cuisine := _primary_cuisine(poi):
        keys.append(f"c:{cuisine}")
    if poi.ward:
        keys.append(f"w:{poi.ward}")
    if poi.tier == "A" or poi.michelin:
        keys.append("f")
    return keys


def _fits(item: Scored, counts: Counter[str], famous_capped: bool) -> bool:
    caps = {"c": CAP_CUISINE, "w": CAP_WARD, "f": CAP_FAMOUS if famous_capped else None}
    for key in _diversity_keys(item.poi):
        cap = caps[key[0]]
        if cap is not None and counts[key] >= cap:
            return False
    return True


def diversify(items: list[Scored], famous_capped: bool) -> list[Scored]:
    """Greedy re-rank of score-ordered items: per window of top items, cap repeats of
    cuisine/ward/famous places. Past ``DIVERSITY_DEPTH`` the plain score order is kept.
    """
    remaining, tail = items[:DIVERSITY_DEPTH], items[DIVERSITY_DEPTH:]
    ordered: list[Scored] = []
    counts: Counter[str] = Counter()
    while remaining:
        if len(ordered) % DIVERSITY_WINDOW == 0:
            counts = Counter()
        pick = next((i for i in remaining if _fits(i, counts, famous_capped)), remaining[0])
        remaining.remove(pick)
        ordered.append(pick)
        counts.update(_diversity_keys(pick.poi))
    return [*ordered, *tail]


def order_items(items: list[Scored], famous_capped: bool) -> list[Scored]:
    """Preferred places first, then keyword hits merged into the section, then the rest;
    score order with diversity within each tier."""
    items = sorted(items, key=lambda s: (-s.score, s.poi.id))
    tiers = (
        [s for s in items if s.preferred],
        [s for s in items if not s.preferred and s.keyword_hit],
        [s for s in items if not s.preferred and not s.keyword_hit],
    )
    return [item for tier in tiers for item in diversify(tier, famous_capped)]


@dataclass(frozen=True)
class _Context:
    hits: KeywordHits
    window: TripWindow
    anchors: list[tuple[float, float]]
    seed: int
    closeness: Closeness

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
            "explore": explore(self.seed, poi.id),
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
    best: tuple[tuple[tuple[float, int], float, bool, int], Scored] | None = None
    for spec in specs:
        found = _hits_for(spec, poi, ctx.hits, ctx.closeness)
        if found is None:
            continue
        scored = ctx.score(poi, spec.key, found, hours, preferred)
        # Specificity first: a required broad interest must not swallow a narrower one.
        rank = (spec.specificity(poi), round(scored.score, 6), spec.required, -spec.order)
        if best is None or rank > best[0]:
            best = (rank, scored)
    if best is None and preferred:
        return ctx.score(poi, PREFERRED_KEY, [Hit("name", "prefer")], hours, True)
    return best[1] if best else None


def _finish(
    spec: SectionSpec,
    items: list[Scored],
    warnings: list[str],
    unsearched: frozenset[str] = frozenset(),
) -> SectionResult | None:
    items = order_items(items, famous_capped=not spec.michelin_driven)
    for request in spec.merged:
        # A companion's words only rank places: the group section answers the wish.
        if request.key in unsearched or request.companion:
            continue
        if not any(hit.raw == request.raw for item in items for hit in item.hits):
            warnings.append(f"Không tìm thấy địa điểm cho “{request.raw}”")
    if items:
        return SectionResult(spec, items)
    if spec.outside is not None:  # a companion's places outside its group, all closed or picked
        return None
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
    seed: int = 0,
    closeness: Closeness | None = None,
    unsearched: frozenset[str] = frozenset(),
) -> tuple[list[SectionResult], list[str]]:
    """Score every candidate, assign it to one section and order each section. ``seed`` picks
    the order of equally relevant places (scoring.explore); ``closeness`` grades semantic
    hits; ``unsearched`` lists free-text requests past the semantic cap (build_specs)."""
    homes: dict[str, str | None] = {}
    pruned: KeywordHits = {}
    ctx = _Context(pruned, window, anchors or [], seed, closeness or {})

    def suggestable(poi_id: str) -> bool:
        """In the candidates, not avoided and open during the trip; picked or not."""
        poi = pois.get(poi_id)
        return poi is not None and not avoided(poi, plan.avoid) and ctx.hours(poi) is not None

    def usable(poi_id: str) -> bool:
        return poi_id not in pinned_ids and suggestable(poi_id)

    # Home and pruning first, on every hit (picked places too), then the SEMANTIC_KEEP closest
    # usable semantic hits of what is left: picking places refills the section from its home.
    found_any: set[str] = set()
    left: set[str] = set()
    companions = {
        r.key: INTEREST_MAP.groups[r.companion]
        for r in plan.requests
        if r.companion and INTEREST_MAP.groups[r.companion].nodes
    }
    for key, found in hits.items():
        close = ctx.closeness.get(key, {})
        if key in companions:
            homes[key], kept = companion_hits(found, pois, companions[key])
        else:
            homes[key], kept = prune_hits(found, pois, close)
        pruned[key] = _closest_semantic(kept, close, usable)
        if any(map(suggestable, kept)):
            found_any.add(key)
        if any(map(usable, pruned[key])):
            left.add(key)
    specs, warnings = build_specs(
        plan, pruned, homes, frozenset(found_any), unsearched, frozenset(left)
    )
    members: dict[str, list[Scored]] = {spec.key: [] for spec in specs}
    for poi in pois.values():
        if poi.id in pinned_ids or avoided(poi, plan.avoid):
            continue
        scored = _best_placement(poi, specs, ctx, poi.id in preferred_ids)
        if scored is None:
            continue
        members.setdefault(scored.section, []).append(scored)
    if PREFERRED_KEY in members and all(spec.key != PREFERRED_KEY for spec in specs):
        # Added after the loop: _best_placement cannot score a spec without group or request.
        last = max((spec.order for spec in specs), default=-1)
        specs.append(SectionSpec(PREFERRED_KEY, PREFERRED_LABEL, False, last + 1))

    results = [_finish(spec, members[spec.key], warnings, unsearched) for spec in specs]
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
        visit_minutes=VISIT_MINUTES.of_place(poi),
    )
