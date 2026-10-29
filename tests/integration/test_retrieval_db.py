"""SQL paths against a real Postgres loaded with data/places_100.parquet.

    uv run --group data python scripts/import_parquet.py data/places_100.parquet \
        --schema retrieval_test
    uv run pytest -m integration

Skipped without DATABASE_URL or when the test schema is empty.
"""

import asyncio
import os
import sys
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from psycopg import AsyncConnection

from app.core.config import Settings
from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.retrieval import names
from app.services.retrieval.config import INTEREST_MAP
from app.services.retrieval.service import retrieve
from app.services.retrieval.text import fold_case, has_diacritics, lexeme_query, norm
from tests.test_retrieval_names_interests import BASE
from tests.test_retrieval_taxonomy import TAXONOMY

pytestmark = pytest.mark.integration
SCHEMA = os.environ.get("RETRIEVAL_TEST_SCHEMA", "retrieval_test")


def run[T](job: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    url = Settings().database_url
    if url is None:
        pytest.skip("DATABASE_URL is not set")

    async def main() -> T:
        conn = await AsyncConnection.connect(
            url.get_secret_value(), autocommit=True, options=f"-c search_path={SCHEMA},public"
        )
        async with conn:
            cur = await conn.execute("SELECT to_regclass('pois') IS NOT NULL")
            row = await cur.fetchone()
            if not row or not row[0]:
                pytest.skip(f"{SCHEMA}.pois does not exist; run scripts/import_parquet.py")
            return await job(conn)

    # psycopg async cannot run on Windows' default Proactor loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(main(), loop_factory=loop_factory)


def retrieval(**extraction: Any) -> RetrievalResponse:
    request = RetrievalRequest.model_validate({"extraction": {**BASE, **extraction}, "limit": 20})
    return run(lambda conn: retrieve(conn, request))


@pytest.mark.parametrize(
    ("query", "status", "poi_id"),
    [
        ("Chợ Bến Thành", "resolved", "cho-ben-thanh-68e3af"),
        ("dinh doc lap", "resolved", "dinh-doc-lap-97d379"),
        ("War Remnants Museum", "resolved", "bao-tang-chung-tich-chien-tranh-08b374"),
        ("Bún thịt nướng", "ambiguous", None),
        ("Landmark 81", "not_found", None),
    ],
)
def test_name_lookup(query: str, status: str, poi_id: str | None) -> None:
    match = run(lambda conn: names.lookup(conn, query))
    assert match.status == status
    if poi_id:
        assert match.hits[0].poi_id == poi_id


def test_pho_is_merged_into_food_and_listed_first() -> None:
    response = retrieval(interests=["food", "phở"])
    food = next(s for s in response.sections if s.key == "food")
    assert food.matched_keywords == ["phở"]
    assert food.items[0].id.startswith("pho")
    assert any("phở" in reason for reason in food.items[0].reasons)


def test_cafe_has_no_bars_and_bar_gets_nightlife() -> None:
    cafe = retrieval(interests=["cafe"])
    assert {i.id for s in cafe.sections for i in s.items} == {
        "fashionista-cafe-bce683",
        "highlands-coffee-ben-van-don-hcm-a754e1",
    }
    bar = retrieval(interests=["bar"])
    assert [s.key for s in bar.sections] == ["nightlife"]
    assert "go2-bar-9cec48" in {i.id for i in bar.sections[0].items}


def test_pinned_avoid_and_status() -> None:
    response = retrieval(
        interests=["shopping"], must_visit=["Dinh Độc Lập"], avoid=["Chợ Bến Thành"]
    )
    assert [p.id for p in response.pinned] == ["dinh-doc-lap-97d379"]
    listed = {i.id for s in response.sections for i in s.items}
    assert "cho-ben-thanh-68e3af" not in listed
    assert retrieval(destination="Hanoi", interests=["food"]).status == "unsupported_destination"
    assert response.meta.data_version == "places_100"


# Words stored "as written" (weight A) in each POI's search document.
WRITTEN_SQL = "SELECT u.lexeme FROM unnest(p.search_lex) AS u WHERE 'A' = ANY(u.weights)"


def test_accent_rule_holds_for_every_colliding_word_in_the_data() -> None:
    """Data-driven: every accented word that shares its folded form with another word in the
    corpus (phở/phố, bò/bộ, ...) matches exactly the POIs that contain it as written, or that
    contain its folded form written without accents. Grows with the data, no word list."""

    async def job(conn: AsyncConnection) -> tuple[list[str], list, list]:
        cur = await conn.execute(f"SELECT DISTINCT w FROM pois p, LATERAL ({WRITTEN_SQL}) AS t(w)")
        groups: defaultdict[str, set[str]] = defaultdict(set)
        for (word,) in await cur.fetchall():
            groups[norm(word)].add(word)
        forms = sorted(w for group in groups.values() if len(group) > 1 for w in group
                       if has_diacritics(w))
        args = (forms, [norm(w) for w in forms], [lexeme_query(w) for w in forms])
        table = "unnest(%s::text[], %s::text[], %s::text[]) AS f(word, folded, q)"
        cur = await conn.execute(
            f"""SELECT f.word, p.id FROM {table} JOIN pois p ON p.search_lex @@ f.q::tsquery
                WHERE NOT EXISTS (SELECT 1 FROM ({WRITTEN_SQL}) AS t(w)
                                  WHERE t.w IN (f.word, f.folded))""",
            args,
        )
        wrong = await cur.fetchall()
        cur = await conn.execute(
            f"""SELECT f.word, p.id FROM {table} CROSS JOIN pois p
                WHERE EXISTS (SELECT 1 FROM ({WRITTEN_SQL}) AS t(w) WHERE t.w = f.word)
                  AND NOT p.search_lex @@ f.q::tsquery""",
            args,
        )
        return forms, wrong, await cur.fetchall()

    forms, wrong, missed = run(job)
    assert forms, "the corpus has no colliding words to check"
    assert wrong == [], f"matched without the word as written: {wrong[:10]}"
    assert missed == [], f"POIs containing the word were not matched: {missed[:10]}"


def test_accented_interest_never_matches_a_different_accented_word() -> None:
    """The reported request: cà phê, phở, văn hóa. No 'Thành phố' / 'Đông Phố' place may be
    explained by 'phở'; phở has no host among the requested sections, so it gets its own,
    listed in the traveller's order."""
    response = retrieval(interests=["cafe", "phở", "culture"])
    assert [s.key for s in response.sections] == ["cafe", "kw:pho", "culture"]
    explained = [
        item for section in response.sections for item in section.items
        if any("phở" in reason for reason in item.reasons)
    ]
    assert explained, "phở matched nothing"
    for item in explained:
        assert "phố" not in fold_case(item.name).split(), item.name



def test_every_category_is_in_the_taxonomy_or_a_declared_gap() -> None:
    """Data-driven: a category outside the taxonomy silently drops out of every section unless
    it is a declared gap; a gap the data no longer has is stale and must be removed."""

    async def job(conn: AsyncConnection) -> set[str | None]:
        cur = await conn.execute("SELECT DISTINCT category FROM pois")
        return {row[0] for row in await cur.fetchall()}

    stored = run(job)
    gaps = set(INTEREST_MAP.gaps)
    assert stored - TAXONOMY.categories - gaps == set()
    assert gaps <= stored, f"gap no longer in the data, remove it: {gaps - stored}"


def test_every_section_item_is_under_its_interest_nodes_and_listed_once() -> None:
    response = retrieval(interests=list(INTEREST_MAP.groups))
    seen: list[str] = []
    for section in response.sections:
        group = INTEREST_MAP.groups[section.key]
        for item in section.items:
            assert group.node_for(item.category) is not None, (section.key, item.id, item.category)
            seen.append(item.id)
    assert seen and len(seen) == len(set(seen))
