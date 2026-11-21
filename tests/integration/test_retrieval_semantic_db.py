"""Semantic search against retrieval_test with its stored embeddings (scripts/import_parquet.py).
No Gemini call: the query vector is a stored place's own vector, or a failing embedder.

    uv run pytest -m integration
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg import AsyncConnection

from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.gemini import GeminiUpstreamError
from app.services.retrieval import semantic
from app.services.retrieval.db import reuse
from app.services.retrieval.service import retrieve
from tests.integration.test_retrieval_db import run
from tests.test_retrieval_names_interests import BASE

pytestmark = pytest.mark.integration
WAR_MUSEUM = "bao-tang-chung-tich-chien-tranh-08b374"
FREE_TEXT = "nơi kể về lịch sử chiến tranh"  # matches no taxonomy group or keyword


def free_text_request() -> RetrievalRequest:
    extraction = {
        **BASE,
        "soft_constraints": [{"field": "other", "operator": "prefer", "value": FREE_TEXT}],
    }
    return RetrievalRequest.model_validate({"extraction": extraction, "limit": 20})


def test_a_place_is_found_by_its_own_description_vector() -> None:
    async def job(conn: AsyncConnection) -> RetrievalResponse:
        cur = await conn.execute("SELECT embedding::text FROM pois WHERE id = %s", (WAR_MUSEUM,))
        row = await cur.fetchone()
        if not row or row[0] is None:
            pytest.skip("retrieval_test has no embeddings; run scripts/import_parquet.py")
        vector = [float(x) for x in row[0].strip("[]").split(",")]

        async def embed(texts: list[str]) -> list[list[float]]:
            return [vector for _ in texts]

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
