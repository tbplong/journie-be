"""Place-name resolution for must_visit / preferred_places / avoid (spec 6.2).

The SQL lookup returns up to five candidates with their best similarity over every name key
(name, English name, ASCII name, seed aliases). ``classify`` decides, without I/O, whether that
is one clear place, several plausible ones, or nothing. It never swaps in a "similar" place.

Exact matching runs on folded keys, so names differing only in accents ("Phở Hùng" /
"Phố Hùng") both match. When the query has diacritics, hits whose accent-preserving key equals
the query (``exact_marked``) win over folded-only ones (accent rule in text.py).
"""

from dataclasses import dataclass
from typing import Literal

from psycopg import AsyncConnection

from app.services.retrieval.text import fold_case, has_diacritics, norm

# Starting thresholds; tune with eval/run_eval.py.
RESOLVE_MIN = 0.6
RESOLVE_GAP = 0.15
AMBIGUOUS_MIN = 0.35
AMBIGUOUS_BAND = 0.15
LOOKUP_LIMIT = 5

NameStatus = Literal["resolved", "ambiguous", "not_found"]


@dataclass(frozen=True)
class NameHit:
    poi_id: str
    similarity: float
    exact: bool
    exact_marked: bool = False  # accent-preserving key equals an accented query


@dataclass(frozen=True)
class NameMatch:
    query: str
    status: NameStatus
    hits: tuple[NameHit, ...]  # one when resolved, several when ambiguous, none otherwise


def classify(query: str, hits: list[NameHit]) -> NameMatch:
    """Classify lookup hits (any order) into resolved / ambiguous / not_found."""
    ranked = sorted(hits, key=lambda hit: (not hit.exact, -hit.similarity))
    exact = [hit for hit in ranked if hit.exact]
    exact = [hit for hit in exact if hit.exact_marked] or exact
    if len(exact) == 1:
        return NameMatch(query, "resolved", (exact[0],))
    if len(exact) > 1:  # branches or namesakes share the exact name
        return NameMatch(query, "ambiguous", tuple(exact))
    if not ranked or ranked[0].similarity < AMBIGUOUS_MIN:
        return NameMatch(query, "not_found", ())
    top = ranked[0].similarity
    runner_up = ranked[1].similarity if len(ranked) > 1 else 0.0
    if top >= RESOLVE_MIN and top - runner_up >= RESOLVE_GAP:
        return NameMatch(query, "resolved", (ranked[0],))
    close = tuple(hit for hit in ranked if hit.similarity >= top - AMBIGUOUS_BAND)
    return NameMatch(query, "ambiguous", close)


LOOKUP_SQL = """
SELECT p.id,
       max(greatest(similarity(k, %(q)s), word_similarity(%(q)s, k))) AS sim,
       bool_or(k = %(q)s) AS exact,
       %(marked)s AND %(qv)s = ANY(p.name_keys_vi) AS exact_marked
FROM pois p CROSS JOIN LATERAL unnest(p.name_keys) AS k
WHERE p.recommendable
GROUP BY p.id
HAVING bool_or(k = %(q)s)
    OR max(greatest(similarity(k, %(q)s), word_similarity(%(q)s, k))) >= %(floor)s
ORDER BY exact DESC, sim DESC, p.id
LIMIT %(limit)s
"""


def _key_params(query: str) -> dict[str, object]:
    return {
        "q": norm(query),
        "qv": fold_case(query),
        # Without diacritics the query says nothing about accents: no preference.
        "marked": any(has_diacritics(word) for word in fold_case(query).split()),
    }


async def lookup(conn: AsyncConnection, query: str) -> NameMatch:
    params = _key_params(query)
    if not params["q"]:
        return NameMatch(query, "not_found", ())
    async with conn.cursor() as cur:
        await cur.execute(LOOKUP_SQL, {**params, "floor": AMBIGUOUS_MIN, "limit": LOOKUP_LIMIT})
        rows = await cur.fetchall()
    return classify(
        query, [NameHit(row[0], float(row[1]), bool(row[2]), bool(row[3])) for row in rows]
    )


# No LIMIT: a chain can have far more than LOOKUP_LIMIT branches. `@>` uses pois_name_keys_idx.
EXACT_SQL = """
SELECT id, %(marked)s AND %(qv)s = ANY(name_keys_vi) AS exact_marked
FROM pois
WHERE name_keys @> ARRAY[%(q)s]::text[]
"""


async def exact_ids(conn: AsyncConnection, query: str) -> list[str]:
    """Every place whose name equals the query, under the same accent rule as ``classify``."""
    params = _key_params(query)
    if not params["q"]:
        return []
    async with conn.cursor() as cur:
        await cur.execute(EXACT_SQL, params)
        rows = await cur.fetchall()
    match = classify(query, [NameHit(row[0], 1.0, True, bool(row[1])) for row in rows])
    return [hit.poi_id for hit in match.hits]
