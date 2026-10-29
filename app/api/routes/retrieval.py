from collections.abc import AsyncIterator
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, Request, status
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.core.errors import PROBLEM_CONTENT_TYPE, ProblemError
from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
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


def _problem(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            PROBLEM_CONTENT_TYPE: {
                "example": {
                    "type": "/problems/<slug>",
                    "title": "...",
                    "status": 0,
                    "detail": "...",
                    "instance": "/retrieval",
                }
            }
        },
    }


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Retrieve POI suggestions for an extraction",
    response_description="Pinned places, disambiguation, and ranked sections",
    responses={503: _problem("`retrieval-not-configured` or `retrieval-db-unavailable`")},
)
async def retrieval(
    body: RetrievalRequest,
    conn: Annotated[AsyncConnection, Depends(get_connection)],
) -> RetrievalResponse:
    """Find Ho Chi Minh City places for the `extraction` returned by POST /chat.

    No LLM is involved: names are matched by trigram similarity, interests by kind, columns,
    and full-text search; every `reason` comes from stored data. `status` is
    `unsupported_destination` for other cities and `empty_extraction` for an all-empty
    extraction. Use `section` + `offset` to page one section ("xem thêm").
    """
    return await retrieve(conn, body)
