from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.core.config import Settings, get_settings
from app.core.errors import PROBLEM_CONTENT_TYPE, ProblemError
from app.schemas.extraction import ExtractionRequest, ExtractionResponse
from app.services.extraction.service import InvalidModelOutputError, extract_constraints
from app.services.gemini import (
    GeminiClient,
    GeminiRateLimitError,
    GeminiTimeoutError,
    GeminiUpstreamError,
)

router = APIRouter(prefix="/chat", tags=["chat"])


def get_gemini_client(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> GeminiClient:
    if settings.gemini_api_key is None:
        raise ProblemError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "llm-not-configured",
            "LLM not configured",
            "GEMINI_API_KEY is not set on the server.",
        )
    return GeminiClient(
        request.app.state.http_client, settings, settings.gemini_api_key.get_secret_value()
    )


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
                    "instance": "/chat",
                }
            }
        },
    }


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Extract travel constraints from free text",
    response_description="Validated, deterministically repaired extraction",
    responses={
        502: _problem("`invalid-llm-output` or `llm-upstream-error`"),
        503: _problem("`llm-not-configured` or `llm-rate-limited` (with Retry-After)"),
        504: _problem("`llm-timeout`"),
    },
)
async def chat(
    body: ExtractionRequest,
    client: Annotated[GeminiClient, Depends(get_gemini_client)],
) -> ExtractionResponse:
    """Run the journie extraction pipeline on one travel request.

    The pipeline never plans an itinerary. Instruction attacks inside `text` yield an empty
    extraction (all fields null/empty, all four `missing_fields`). Check
    `extraction.needs_clarification` before planning; `issues` lists diagnostics
    that the deterministic repairs could not resolve.
    """
    try:
        return await extract_constraints(client, body.text, body.reference_date)
    except InvalidModelOutputError as exc:
        raise ProblemError(
            status.HTTP_502_BAD_GATEWAY,
            "invalid-llm-output",
            "Invalid LLM output",
            f"The model did not return a valid extraction ({exc}, "
            f"finish_reason={exc.finish_reason}). Retrying may succeed.",
        ) from exc
    except GeminiTimeoutError as exc:
        raise ProblemError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "llm-timeout",
            "LLM timeout",
            "The model did not respond in time. Retry later.",
        ) from exc
    except GeminiRateLimitError as exc:
        raise ProblemError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "llm-rate-limited",
            "LLM quota exceeded",
            "The model provider's quota or rate limit was reached. Retry later.",
            headers={"Retry-After": exc.retry_after or "30"},
        ) from exc
    except GeminiUpstreamError as exc:
        raise ProblemError(
            status.HTTP_502_BAD_GATEWAY,
            "llm-upstream-error",
            "LLM provider error",
            str(exc),
        ) from exc
