"""Extraction pipeline: prompt -> Gemini (native JSON schema) -> strict validation -> repair."""

import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.schemas.extraction import (
    RESPONSE_SCHEMA,
    ConsistencyIssue,
    Extraction,
    ExtractionMeta,
    ExtractionResponse,
    TokenUsage,
)
from app.services.extraction.processing import (
    DIAGNOSTICS_VERSION,
    PROCESSING_VERSION,
    consistency_issues,
    repair_extraction,
)
from app.services.extraction.prompt import PROMPT_VERSION, build_system_prompt
from app.services.gemini import GeminiClient

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


class InvalidModelOutputError(Exception):
    """The model answered, but not with a schema-valid extraction."""

    def __init__(self, reason: str, finish_reason: str | None) -> None:
        super().__init__(reason)
        self.finish_reason = finish_reason


def today() -> date:
    return datetime.now(TIMEZONE).date()


async def extract_constraints(
    client: GeminiClient, text: str, reference_date: date | None = None
) -> ExtractionResponse:
    """Extract travel constraints from one user message.

    Args:
        client: Configured Gemini client.
        text: The user's travel request; treated as untrusted data by the prompt.
        reference_date: Anchor for relative dates; defaults to today in Asia/Ho_Chi_Minh.

    Raises:
        InvalidModelOutputError: The response was empty, blocked, truncated, or schema-invalid.
        GeminiError: The Gemini request itself failed.
    """
    reference = reference_date or today()
    started = time.perf_counter()
    generation = await client.generate_json(build_system_prompt(reference), text, RESPONSE_SCHEMA)
    if not generation.text:
        raise InvalidModelOutputError("empty response", generation.finish_reason)
    try:
        parsed = Extraction.model_validate_json(generation.text)
    except ValidationError as exc:
        # Log only error locations/types, never the user text or model output.
        locations = [".".join(map(str, error["loc"])) for error in exc.errors()][:5]
        logger.warning(
            "Gemini output failed validation (finish_reason=%s): %s",
            generation.finish_reason,
            locations,
        )
        raise InvalidModelOutputError("schema-invalid response", generation.finish_reason) from exc

    extraction, repairs = repair_extraction(parsed)
    issues = [ConsistencyIssue.model_validate(issue) for issue in consistency_issues(extraction)]
    return ExtractionResponse(
        extraction=extraction,
        repairs=repairs,
        issues=issues,
        meta=ExtractionMeta(
            model=client.model,
            resolved_model=generation.resolved_model,
            prompt_version=PROMPT_VERSION,
            processing_version=PROCESSING_VERSION,
            diagnostics_version=DIAGNOSTICS_VERSION,
            reference_date=reference,
            latency_ms=round((time.perf_counter() - started) * 1000),
            usage=TokenUsage(
                input_tokens=generation.input_tokens,
                output_tokens=generation.output_tokens,
                total_tokens=generation.total_tokens,
                reasoning_tokens=generation.reasoning_tokens,
            ),
        ),
    )
