"""Place-name resolution for must_visit / preferred_places / avoid (spec 6.2).

The SQL lookup returns up to five candidates with their best similarity over every name key
(name, English name, ASCII name, seed aliases). ``classify`` decides, without I/O, whether that
is one clear place, several plausible ones, or nothing. It never swaps in a "similar" place.

A name matches ("exact") when a key equals the query or starts with it as whole words: the
branch ("Katinat (Đường Hàm Nghi)") or tagline ("Thảo Cầm Viên - Sài gòn Zoo") follows the
name, so every branch of a chain matches its bare name, while a street inside a name ("Cộng Cà
Phê (152 Đường Chợ Lớn)" for "Chợ Lớn") or the same words in another order ("Quán Chay Chị Củ"
for "Củ Chi") do not. Fuzzy hits use the symmetric ``similarity()``, so extra words count
against a candidate.

Name matching runs on folded keys, so names differing only in accents ("Phở Hùng" /
"Phố Hùng") both match. When the query has diacritics, hits whose accent-preserving key matches
the query (``exact_marked``) win over folded-only ones, and fuzzy similarity is measured on the
accent-preserving keys (accent rule in text.py).
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


# A key equals the query or starts with it followed by a space (branch, tagline).
NAME_MATCH = (
    "EXISTS (SELECT 1 FROM unnest({keys}) AS k WHERE k = {q} OR starts_with(k, {q} || ' '))"
)
FOLDED_MATCH = NAME_MATCH.format(keys="p.name_keys", q="%(q)s")
MARKED_MATCH = f"%(marked)s AND {NAME_MATCH.format(keys='p.name_keys_vi', q='%(qv)s')}"

# shortcut: similarity() and starts_with() over unnested keys cannot use an index, so every name
# scans every recommendable place (<= 60 names per request); fine below ~20k places, beyond that
# pre-filter with the index-backed `%` operator on a trigram index and score only the rows it
# keeps.
LOOKUP_SQL = f"""
SELECT id, sim, exact, exact_marked FROM (
    SELECT p.id, p.name,
           COALESCE((
               SELECT max(similarity(k, %(qs)s))
               FROM unnest(CASE WHEN %(marked)s THEN p.name_keys_vi ELSE p.name_keys END) AS k
           ), 0) AS sim,
           {FOLDED_MATCH} AS exact,
           {MARKED_MATCH} AS exact_marked
    FROM pois p
    WHERE p.recommendable
) AS scored
WHERE exact OR sim >= %(floor)s
ORDER BY exact_marked DESC, exact DESC, sim DESC, length(name), id
LIMIT %(limit)s
"""


def _key_params(query: str) -> dict[str, object]:
    marked = any(has_diacritics(word) for word in fold_case(query).split())
    return {
        "q": norm(query),
        "qv": fold_case(query),
        # Without diacritics the query says nothing about accents: no preference.
        "marked": marked,
        # Fuzzy similarity on the accent-preserving keys when accents were given.
        "qs": fold_case(query) if marked else norm(query),
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


# No LIMIT: a chain can have far more than LOOKUP_LIMIT branches.
EXACT_SQL = f"""
SELECT p.id, {MARKED_MATCH} AS exact_marked
FROM pois p
WHERE {FOLDED_MATCH}
"""


async def exact_ids(conn: AsyncConnection, query: str) -> list[str]:
    """Every place whose name matches the query (every branch of a chain), under the same
    accent rule as ``classify``."""
    params = _key_params(query)
    if not params["q"]:
        return []
    async with conn.cursor() as cur:
        await cur.execute(EXACT_SQL, params)
        rows = await cur.fetchall()
    match = classify(query, [NameHit(row[0], 1.0, True, bool(row[1])) for row in rows])
    return [hit.poi_id for hit in match.hits]
