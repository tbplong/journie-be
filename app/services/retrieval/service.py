"""retrieve(): extraction -> pinned places, disambiguation and ranked sections. No LLM."""

import time

from psycopg import AsyncConnection

from app.schemas.extraction import Extraction
from app.schemas.retrieval import (
    Disambiguation,
    NameField,
    NotFound,
    PoiCard,
    RetrievalMeta,
    RetrievalRequest,
    RetrievalResponse,
    Section,
)
from app.services.retrieval import candidates, names
from app.services.retrieval.config import INTEREST_MAP, KEYWORD_MAP
from app.services.retrieval.hours import TripWindow, check_hours, decide, trip_window
from app.services.retrieval.interests import plan_interests
from app.services.retrieval.poi import Poi
from app.services.retrieval.sections import ANY_TIME, Scored, SectionResult, assemble, to_card

RETRIEVAL_VERSION = "retrieval-v0"
SUPPORTED_DESTINATION = "Ho Chi Minh City"


def is_empty(extraction: Extraction) -> bool:
    """True for the all-null output extraction returns for instruction attacks."""
    scalars = (
        "destination", "start_date", "end_date", "duration_days", "budget_vnd",
        "available_start", "available_end",
    )
    lists = (
        "interests", "must_visit", "avoid", "preferred_places",
        "hard_constraints", "soft_constraints",
    )
    return all(getattr(extraction, name) is None for name in scalars) and not any(
        getattr(extraction, name) for name in lists
    )


def _meta(started: float, data_version: str | None) -> RetrievalMeta:
    return RetrievalMeta(
        retrieval_version=RETRIEVAL_VERSION,
        interest_map_version=INTEREST_MAP.version,
        keyword_map_version=KEYWORD_MAP.version,
        data_version=data_version,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


def _bare(status: str, started: float, warnings: list[str]) -> RetrievalResponse:
    return RetrievalResponse(
        status=status,  # type: ignore[arg-type]
        pinned=[],
        disambiguation=[],
        not_found=[],
        sections=[],
        warnings=warnings,
        meta=_meta(started, None),
    )


def name_card(poi: Poi, similarity: float, window: TripWindow) -> PoiCard:
    """Card for a place found by name: pinned or a disambiguation candidate.

    Pinned places skip the hours filter, so a trusted 'closed' is a flag, not an exclusion.
    """
    check = check_hours(poi.opening_hours, window if window.active else ANY_TIME)
    flags: list[str] = []
    if window.active:
        decision, flags = decide(check, poi.hours_confidence, poi.hours_contradicted)
        if decision == "exclude":
            flags = [*flags, "hours_closed"]
    parts = {"name": round(similarity, 3)}
    return to_card(Scored(poi, "pinned", [], check, flags, parts, round(similarity, 4), False))


def _page(result: SectionResult, offset: int, limit: int) -> Section:
    spec = result.spec
    return Section(
        key=spec.key,
        label=spec.label,
        required=spec.required,
        total_matches=len(result.items),
        items=[to_card(item) for item in result.items[offset : offset + limit]],
        matched_keywords=[request.raw for request in spec.merged],
        note=spec.note,
    )


async def retrieve(conn: AsyncConnection, request: RetrievalRequest) -> RetrievalResponse:
    started = time.perf_counter()
    extraction = request.extraction
    if is_empty(extraction):
        return _bare("empty_extraction", started, [])
    if extraction.destination not in (None, SUPPORTED_DESTINATION):
        return _bare(
            "unsupported_destination",
            started,
            [f"Chưa hỗ trợ “{extraction.destination}”: hiện chỉ có dữ liệu TP.HCM"],
        )
    warnings: list[str] = []
    if extraction.destination is None:
        warnings.append("Chưa rõ điểm đến; đang gợi ý địa điểm tại TP.HCM")

    cuisines, data_version = await candidates.data_info(conn)
    plan = plan_interests(extraction, cuisines)
    window = trip_window(
        extraction.start_date,
        extraction.end_date,
        extraction.duration_days,
        extraction.available_start,
        extraction.available_end,
    )

    # Names: must_visit -> pinned, preferred -> boosted, avoid -> excluded.
    queries: list[tuple[NameField, str]] = [
        *(("must_visit", q) for q in extraction.must_visit),
        *(("preferred_places", q) for q in extraction.preferred_places),
        *(("avoid", q) for q in plan.avoid.names),
    ]
    pinned: dict[str, float] = {}
    preferred: set[str] = set()
    avoid_ids: set[str] = set()
    ambiguous: list[tuple[NameField, names.NameMatch]] = []
    not_found: list[NotFound] = []
    for field, query in dict.fromkeys(queries):
        match = await names.lookup(conn, query)
        if field == "avoid" and any(hit.exact for hit in match.hits):
            # Every branch sharing the exact name is avoided, not just the lookup's top few.
            avoid_ids.update(await names.exact_ids(conn, query))
        elif match.status == "resolved":
            hit = match.hits[0]
            if field == "must_visit":
                pinned.setdefault(hit.poi_id, hit.similarity)
            elif field == "preferred_places":
                preferred.add(hit.poi_id)
            else:
                avoid_ids.add(hit.poi_id)
        elif match.status == "ambiguous":
            ambiguous.append((field, match))
        else:
            not_found.append(NotFound(query=query, field=field))
    if conflict := set(pinned) & avoid_ids:
        warnings.append(
            f"{len(conflict)} địa điểm vừa bắt buộc vừa cần tránh; vẫn giữ trong danh sách bắt buộc"
        )

    params = candidates.eligibility(plan.avoid, avoid_ids)
    hits = {
        req.key: await candidates.keyword_hits(conn, req, params)
        for req in plan.requests
        if req.group is None
    }
    group_keys = [req.group for req in plan.requests if req.group] or list(INTEREST_MAP.defaults)
    nodes = {node for key in group_keys for node in INTEREST_MAP.groups[key].nodes}
    hit_ids = {poi_id for found in hits.values() for poi_id in found}
    pois = await candidates.fetch_pois(conn, nodes, hit_ids | preferred, params)

    card_ids = [*pinned, *request.selected_ids, *(h.poi_id for _, m in ambiguous for h in m.hits)]
    by_id = await candidates.fetch_by_ids(conn, list(dict.fromkeys(card_ids)))
    anchors = [
        (poi.latitude, poi.longitude)
        for poi_id in [*pinned, *request.selected_ids]
        if (poi := by_id.get(poi_id)) and poi.latitude is not None and poi.longitude is not None
    ]
    results, section_warnings = assemble(
        plan, pois, hits, window, frozenset(preferred), frozenset(pinned), anchors
    )
    if request.section is not None:
        results = [result for result in results if result.spec.key == request.section]

    return RetrievalResponse(
        status="ok",
        pinned=[name_card(by_id[i], sim, window) for i, sim in pinned.items() if i in by_id],
        disambiguation=[
            Disambiguation(
                query=match.query,
                field=field,
                candidates=[
                    name_card(by_id[hit.poi_id], hit.similarity, window)
                    for hit in match.hits
                    if hit.poi_id in by_id
                ],
            )
            for field, match in ambiguous
        ],
        not_found=not_found,
        sections=[_page(result, request.offset, request.limit) for result in results],
        warnings=list(dict.fromkeys([*warnings, *section_warnings])),
        meta=_meta(started, data_version),
    )
