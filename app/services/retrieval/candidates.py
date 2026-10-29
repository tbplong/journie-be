"""SQL candidate generation (spec 6.3-6.5): category candidates and keyword hits.

Eligibility (recommendable, inside the region, not avoided by id or category) is applied in SQL;
hours, scoring and section assignment happen in Python on the returned rows.
"""

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.services.retrieval.interests import AvoidRules, InterestRequest
from app.services.retrieval.poi import POI_COLUMNS, Poi
from app.services.retrieval.scoring import Evidence
from app.services.retrieval.text import fold_case, lexeme_query, norm

# A name hit needs the query as a phrase in the name (accent rule, text.py), or for typos this
# word_similarity on the accent-preserving name. 0.6 was too loose: "banh mi" scored 0.625
# against "banh cuon". On folded text "pho" scores 1.0 against "thanh pho", hence name_text.
NAME_HIT_MIN = 0.8


def in_nodes(param: str) -> str:
    """SQL: the row's category is under one of the taxonomy nodes in %(param)s. A sub node
    (food.cafe) equals a category, a top node (food) equals category_top; both are indexed."""
    return f"COALESCE(category = ANY(%({param})s) OR category_top = ANY(%({param})s), false)"


ELIGIBLE = f"""
    recommendable AND NOT outside_region
    AND NOT (id = ANY(%(avoid_ids)s))
    AND NOT {in_nodes("avoid_nodes")}
"""


def eligibility(avoid: AvoidRules, avoid_ids: set[str]) -> dict[str, Any]:
    return {"avoid_ids": sorted(avoid_ids), "avoid_nodes": sorted(avoid.nodes)}


async def fetch_pois(
    conn: AsyncConnection, nodes: set[str], ids: set[str], params: dict[str, Any]
) -> dict[str, Poi]:
    """Eligible POIs under the given taxonomy nodes, plus the given ids (if eligible)."""
    if not nodes and not ids:
        return {}
    sql = f"""
        SELECT {", ".join(POI_COLUMNS)} FROM pois
        WHERE {ELIGIBLE} AND ({in_nodes("nodes")} OR id = ANY(%(ids)s))
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, {**params, "nodes": sorted(nodes), "ids": sorted(ids)})
        return {row["id"]: Poi.from_row(row) for row in await cur.fetchall()}


async def fetch_by_ids(conn: AsyncConnection, ids: list[str]) -> dict[str, Poi]:
    """POIs by id with no eligibility filter (pinned and disambiguation cards)."""
    if not ids:
        return {}
    sql = f"SELECT {', '.join(POI_COLUMNS)} FROM pois WHERE id = ANY(%(ids)s)"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, {"ids": ids})
        return {row["id"]: Poi.from_row(row) for row in await cur.fetchall()}


STRUCTURED_SQL = f"""
    SELECT id FROM pois
    WHERE {ELIGIBLE}
      AND (michelin = ANY(%(michelin)s) OR cuisine && %(cuisine)s OR features && %(features)s)
"""

# A name hit outranks a hit elsewhere in the search document (cuisine/features/description).
# The lexeme matches use GIN indexes; the word_similarity typo check is evaluated per eligible
# row. Fine at the expected <100k POIs; beyond that, switch it to the index-backed `<%` operator
# with pg_trgm.word_similarity_threshold set on the connection.
TEXT_SQL = f"""
    SELECT id, name_hit FROM (
        SELECT id, search_lex,
               name_lex @@ %(tsq)s::tsquery
               OR word_similarity(%(q)s, name_text) >= %(name_min)s AS name_hit
        FROM pois
        WHERE {ELIGIBLE}
    ) AS scored
    WHERE name_hit OR search_lex @@ %(tsq)s::tsquery
"""


async def keyword_hits(
    conn: AsyncConnection, request: InterestRequest, params: dict[str, Any]
) -> dict[str, Evidence]:
    hits: dict[str, Evidence] = {}
    async with conn.cursor() as cur:
        if request.keyword is not None:
            columns = request.keyword.filter
            await cur.execute(
                STRUCTURED_SQL,
                {
                    **params,
                    "michelin": list(columns.get("michelin", ())),
                    "cuisine": list(columns.get("cuisine", ())),
                    "features": list(columns.get("features", ())),
                },
            )
            hits.update({row[0]: "structured" for row in await cur.fetchall()})
        # Search with the traveller's own spelling: aliases and `text` are folded, which would
        # throw away the accents the accent rule needs.
        searches_text = request.text is not None or (request.keyword and request.keyword.text)
        tsquery = lexeme_query(request.raw) if searches_text else None
        if tsquery:
            await cur.execute(
                TEXT_SQL,
                {**params, "tsq": tsquery, "q": fold_case(request.raw), "name_min": NAME_HIT_MIN},
            )
            for poi_id, name_hit in await cur.fetchall():
                hits.setdefault(poi_id, "name" if name_hit else "text")
    return hits


async def known_cuisines(conn: AsyncConnection) -> dict[str, str]:
    """Normalized cuisine value -> stored value, for matching interests like 'ramen'."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT unnest(cuisine) FROM pois")
        return {norm(value): value for (value,) in await cur.fetchall()}


async def data_version(conn: AsyncConnection) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT max(data_version) FROM pois")
        row = await cur.fetchone()
        return row[0] if row else None
