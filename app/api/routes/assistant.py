import math
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.routes.chat import gemini_problem, get_gemini_client
from app.api.routes.retrieval import get_connection, get_embedder
from app.core.config import Settings, get_settings
from app.core.errors import ProblemError, problem_doc
from app.core.ratelimit import RateLimiter
from app.schemas.assistant import AssistantRequest, AssistantResponse
from app.services.assistant.service import answer
from app.services.extraction.service import InvalidModelOutputError
from app.services.gemini import GeminiClient, GeminiError
from app.services.retrieval.db import Connect
from app.services.retrieval.semantic import Embed

router = APIRouter(prefix="/assistant", tags=["assistant"])


def get_optional_connection(request: Request) -> Connect | None:
    """The POI database, or None when DATABASE_URL is not set: the assistant then answers
    without places instead of failing."""
    if getattr(request.app.state, "db_pool", None) is None:
        return None
    return get_connection(request)


async def rate_limit(request: Request) -> None:
    """429 once the client, or all clients together, asked too often (Settings
    assistant_*_rate_per_minute). Async, so the buckets are only touched from the event loop.
    Behind a proxy, run uvicorn with --proxy-headers so the client IP is the traveller's."""
    limiter: RateLimiter | None = getattr(request.app.state, "assistant_limiter", None)
    if limiter is None:
        return
    wait = limiter.acquire(request.client.host if request.client else "unknown")
    if wait > 0:
        raise ProblemError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate-limited",
            "Too many requests",
            "Too many questions in a short time. Retry after the Retry-After seconds.",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Answer a traveller's question in Journie's voice",
    response_description="A short grounded reply, with the places and guides it names",
    dependencies=[Depends(rate_limit)],
    responses={
        429: problem_doc("`rate-limited` (with Retry-After)", "/assistant"),
        502: problem_doc("`invalid-llm-output` or `llm-upstream-error`", "/assistant"),
        503: problem_doc(
            "`llm-not-configured` or `llm-rate-limited` (with Retry-After)", "/assistant"
        ),
        504: problem_doc("`llm-timeout`", "/assistant"),
    },
)
async def assistant(
    body: AssistantRequest,
    client: Annotated[GeminiClient, Depends(get_gemini_client)],
    connect: Annotated[Connect | None, Depends(get_optional_connection)],
    embed: Annotated[Embed | None, Depends(get_embedder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssistantResponse:
    """Chat with Journie about a trip in Ho Chi Minh City, Hanoi, Da Nang or Hue.

    The last user message is embedded and matched against place descriptions of the city it
    names, else of `destination` (default Ho Chi Minh City); without embeddings, full-text
    search is used, and without the database the reply names no place. A few sections of
    Journie's guides (how the app works, day pacing, visit lengths) go in as context too. The
    reply recommends only those places; `places` lists the ones it names. Messages, trip and
    place texts are treated as data, so instructions inside them are ignored.

    Past ASSISTANT_CLIENT_RATE_PER_MINUTE questions a minute from one client (default 6), or
    ASSISTANT_TOTAL_RATE_PER_MINUTE from all clients (default 20), the answer is 429 with
    Retry-After.
    """
    try:
        return await answer(
            client,
            body,
            connect,
            embed,
            timeout=settings.assistant_timeout_seconds,
            fallback_model=settings.assistant_fallback_model,
        )
    except InvalidModelOutputError as exc:
        raise ProblemError(
            status.HTTP_502_BAD_GATEWAY,
            "invalid-llm-output",
            "Invalid LLM output",
            f"The model did not return a valid answer ({exc}, "
            f"finish_reason={exc.finish_reason}). Retrying may succeed.",
        ) from exc
    except GeminiError as exc:
        raise gemini_problem(exc) from exc
