"""Travel-constraint extraction contract and HTTP models.

``Constraint`` and ``Extraction`` mirror journie-data ``src/journie-llm/schema.py``
field for field. Their JSON Schema is sent to Gemini as ``responseJsonSchema``, so do
not add descriptions or defaults here: that would change the benchmarked request.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Constraint(StrictModel):
    field: Literal[
        "destination",
        "start_date",
        "end_date",
        "duration_days",
        "budget_vnd",
        "available_start",
        "available_end",
        "interests",
        "must_visit",
        "avoid",
        "preferred_places",
        "visit_duration_min",
        "other",
    ]
    operator: Literal["eq", "min", "max", "include", "exclude", "prefer"]
    value: str


class Extraction(StrictModel):
    destination: str | None
    start_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    duration_days: int | None = Field(ge=1)
    budget_vnd: int | None = Field(ge=0)
    budget_scope: Literal["total", "per_person"] | None
    available_start: str | None = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    available_end: str | None = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    interests: list[str]
    must_visit: list[str]
    avoid: list[str]
    preferred_places: list[str]
    hard_constraints: list[Constraint]
    soft_constraints: list[Constraint]
    missing_fields: list[
        Literal[
            "destination",
            "travel_dates_or_duration",
            "budget",
            "available_time",
        ]
    ]
    possible_conflicts: list[
        Literal[
            "date_order",
            "time_order",
            "duration_disagreement",
            "budget_disagreement",
            "must_visit_avoid",
            "destination_disagreement",
        ]
    ]
    needs_clarification: bool

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_calendar_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value


RESPONSE_SCHEMA = Extraction.model_json_schema()


class ExtractionRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=4000,
        description="The traveller's free-text request, in Vietnamese or English.",
        examples=["Đi Đà Lạt 3 ngày từ 2026-11-20, tối đa 3tr, thích cà phê, tránh chợ đêm"],
    )
    reference_date: date | None = Field(
        default=None,
        description="Date used to resolve relative dates such as 'next Friday'. "
        "Defaults to today in Asia/Ho_Chi_Minh.",
    )

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ConsistencyIssue(BaseModel):
    code: str
    field: str
    severity: Literal["error", "review"]
    detail: str


class TokenUsage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None


class ExtractionMeta(BaseModel):
    model: str
    resolved_model: str | None
    prompt_version: str
    processing_version: str
    diagnostics_version: str
    reference_date: date
    latency_ms: int
    usage: TokenUsage


class ExtractionResponse(BaseModel):
    extraction: Extraction
    repairs: list[str] = Field(
        description="Deterministic repairs applied to the model output, in order."
    )
    issues: list[ConsistencyIssue] = Field(
        description="Read-only consistency diagnostics on the final extraction."
    )
    meta: ExtractionMeta
