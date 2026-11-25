"""Semantic search against retrieval_test with its stored embeddings (scripts/import_parquet.py).
No Gemini call: the query vector is a stored place's own vector, or a failing embedder.

    uv run pytest -m integration
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg import AsyncConnection

from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.gemini import GeminiUpstreamError
from app.services.retrieval import semantic
from app.services.retrieval.db import reuse
from app.services.retrieval.sections import NO_MATCH_DEFAULTS
from app.services.retrieval.service import retrieve
from app.services.retrieval.taxonomy import top
from tests.integration.test_retrieval_db import run
from tests.test_retrieval_names_interests import BASE

pytestmark = pytest.mark.integration
WAR_MUSEUM = "bao-tang-chung-tich-chien-tranh-08b374"
FREE_TEXT = "nơi kể về lịch sử chiến tranh"  # matches no taxonomy group or keyword


def free_text_request(**fields: Any) -> RetrievalRequest:
    extraction = {
        **BASE,
        "soft_constraints": [{"field": "other", "operator": "prefer", "value": FREE_TEXT}],
    }
    return RetrievalRequest.model_validate({"extraction": extraction, "limit": 20, **fields})


async def stored_vector_embed(conn: AsyncConnection) -> semantic.Embed:
    """An embedder answering with the war museum's stored vector."""
    cur = await conn.execute("SELECT embedding::text FROM pois WHERE id = %s", (WAR_MUSEUM,))
    row = await cur.fetchone()
    if not row or row[0] is None:
        pytest.skip("retrieval_test has no embeddings; run scripts/import_parquet.py")
    vector = [float(x) for x in row[0].strip("[]").split(",")]

    async def embed(texts: list[str]) -> list[list[float]]:
        return [vector for _ in texts]

    return embed


def test_a_place_is_found_by_its_own_description_vector() -> None:
    async def job(conn: AsyncConnection) -> RetrievalResponse:
        embed = await stored_vector_embed(conn)
        return await retrieve(reuse(conn), free_text_request(), embed)

    response = run(job)
    section = next(s for s in response.sections if s.key.startswith("kw:"))
    assert section.items[0].id == WAR_MUSEUM
    assert "Gợi ý theo mô tả" in section.items[0].reasons


def test_an_embedding_failure_falls_back_to_full_text_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic, "QUERY_CACHE", semantic.QueryCache())  # no cached vector

    async def failing(texts: list[str]) -> list[list[float]]:
        raise GeminiUpstreamError("down")

    async def job(conn: AsyncConnection) -> RetrievalResponse:
        return await retrieve(reuse(conn), free_text_request(), failing)

    response = run(job)
    assert response.status == "ok"
    assert any("theo ý nghĩa" in warning for warning in response.warnings)
    # No description holds the phrase as written: the defaults are shown, and say so.
    assert [s.key for s in response.sections] == ["food", "culture"]
    assert NO_MATCH_DEFAULTS.format(FREE_TEXT) in response.warnings


def test_no_connection_is_held_while_waiting_for_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic, "QUERY_CACHE", semantic.QueryCache())  # force the call
    borrowed = 0
    seen: list[int] = []

    async def job(conn: AsyncConnection) -> RetrievalResponse:
        @asynccontextmanager
        async def connect() -> AsyncIterator[AsyncConnection]:
            nonlocal borrowed
            borrowed += 1
            try:
                yield conn
            finally:
                borrowed -= 1

        async def embed(texts: list[str]) -> list[list[float]]:
            seen.append(borrowed)
            raise GeminiUpstreamError("only the borrow count matters")

        return await retrieve(connect, free_text_request(), embed)

    assert run(job).status == "ok"
    assert seen == [0]


def test_picking_places_keeps_the_home_and_then_falls_back_to_the_defaults() -> None:
    """N14/N15: picked places leave the section, the rest stay, the main group does not change.
    N16: once every place is picked, the default sections are shown with no false warning."""

    async def all_items(conn: AsyncConnection, embed: semantic.Embed, picked: list[str]):
        response = await retrieve(reuse(conn), free_text_request(selected_ids=picked), embed)
        found = next((s for s in response.sections if s.key.startswith("kw:")), None)
        items = list(found.items) if found else []
        while found and len(items) < found.total_matches:
            request = free_text_request(selected_ids=picked, section=found.key, offset=len(items))
            items += (await retrieve(reuse(conn), request, embed)).sections[0].items
        return response, [(item.id, item.category) for item in items]

    async def job(conn: AsyncConnection):
        embed = await stored_vector_embed(conn)
        _, before = await all_items(conn, embed, [])
        picked = [poi_id for poi_id, _ in before[:5]]
        _, after = await all_items(conn, embed, picked)
        every = [poi_id for poi_id, _ in before][:50]
        return before, picked, after, *(await all_items(conn, embed, every))

    before, picked, after, final, rest = run(job)
    assert len(before) > 5, before
    homes = {top(category) for _, category in before}
    assert {top(category) for _, category in after} <= homes, (homes, after)
    assert {i for i, _ in before} - set(picked) <= {i for i, _ in after}
    if len(before) <= 50:
        assert not rest
        assert [s.key for s in final.sections] == ["food", "culture"]
        assert all(s.items for s in final.sections)
        assert not [w for w in final.warnings if "Không tìm thấy" in w], final.warnings
        assert NO_MATCH_DEFAULTS.format(FREE_TEXT) not in final.warnings
