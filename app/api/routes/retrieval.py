from collections.abc import AsyncIterator
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Request, status
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.core.config import Settings, get_settings
from app.core.errors import ProblemError, problem_doc
from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.gemini import embed_all
from app.services.retrieval.semantic import Embed
from app.services.retrieval.service import retrieve

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


def _unavailable(exc: Exception) -> ProblemError:
    return ProblemError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "retrieval-db-unavailable",
        "POI database unavailable",
        f"The POI database could not be reached ({type(exc).__name__}). Retry later.",
        headers={"Retry-After": "5"},
    )


async def get_connection(request: Request) -> AsyncIterator[AsyncConnection]:
    pool: AsyncConnectionPool | None = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ProblemError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "retrieval-not-configured",
            "Retrieval not configured",
            "DATABASE_URL is not set on the server.",
        )
    try:
        async with pool.connection() as conn:
            yield conn
    except (PoolTimeout, psycopg.OperationalError) as exc:
        raise _unavailable(exc) from exc


def get_embedder(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Embed | None:
    """Query embeddings for semantic search: GEMINI_API_KEY first, GEMINI_API_KEY2 on 429
    (the import script uses them the other way round). None when no key is set."""
    keys = [k.get_secret_value() for k in (settings.gemini_api_key, settings.gemini_api_key2) if k]
    if not keys:
        return None
    http = request.app.state.http_client

    async def embed(texts: list[str]) -> list[list[float]]:
        return await embed_all(http, settings, texts, keys)

    return embed


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Retrieve POI suggestions for an extraction",
    response_description="Pinned places, disambiguation, and ranked sections",
    responses={
        503: problem_doc(
            "`retrieval-not-configured` or `retrieval-db-unavailable`", "/retrieval"
        )
    },
)
async def retrieval(
    body: RetrievalRequest,
    conn: Annotated[AsyncConnection, Depends(get_connection)],
    embed: Annotated[Embed | None, Depends(get_embedder)],
) -> RetrievalResponse:
    """Find Ho Chi Minh City places for the `extraction` returned by POST /chat.

    Names are matched by trigram similarity, interests by kind, columns and full-text search.
    Free-text interests (no taxonomy or keyword match, no negation) are also searched by
    meaning: Gemini embeddings of the words against embedded place descriptions; those cards
    say "Gợi ý theo mô tả". Every other `reason` comes from stored data. `status` is
    `unsupported_destination` for other cities and `empty_extraction` for an all-empty
    extraction. Use `section` + `offset` to page one section ("xem thêm").
    """
    return await retrieve(conn, body, embed)
