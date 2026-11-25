"""Journie assistant request/response models (POST /assistant)."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGES = 12
MAX_MESSAGE_CHARS = 1200
# The trip is pasted into the prompt as data; this bounds the prompt (and the cost) per request.
MAX_TRIP_CHARS = 3000

Destination = Literal["Ho Chi Minh City", "Hanoi", "Da Nang", "Hue"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssistantMessage(StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("text")
    @classmethod
    def not_blank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("text must not be blank")
        return text


class TripDay(StrictModel):
    date: str
    stops: list[str]


class TripContext(StrictModel):
    title: str | None = None
    city: str | None = None
    days: list[TripDay] | None = None


class AssistantRequest(StrictModel):
    messages: list[AssistantMessage] = Field(
        min_length=1,
        max_length=MAX_MESSAGES,
        description="The conversation, oldest first; the last message is the traveller's.",
    )
    destination: Destination | None = Field(
        default=None, description="City the places come from; null means Ho Chi Minh City."
    )
    trip: TripContext | None = Field(
        default=None,
        description=(
            f"The trip on screen, as data (at most {MAX_TRIP_CHARS} characters as compact JSON)."
        ),
    )
    language: Literal["vi", "en"] = Field(default="vi", description="Language of the reply.")

    @model_validator(mode="after")
    def check_conversation(self) -> "AssistantRequest":
        if self.messages[-1].role != "user":
            raise ValueError("the last message must have role 'user'")
        if self.trip is not None and len(trip_json(self.trip)) > MAX_TRIP_CHARS:
            raise ValueError(f"trip is longer than {MAX_TRIP_CHARS} characters as JSON")
        return self


def trip_json(trip: TripContext) -> str:
    """The trip as compact JSON, measured the way the frontend's JSON.stringify writes it."""
    return json.dumps(trip.model_dump(exclude_none=True), ensure_ascii=False, separators=(",", ":"))


class AssistantPlace(BaseModel):
    id: str
    name: str
    category: str = Field(description="Taxonomy value top.sub, e.g. entertainment.nightlife.")
    area: str | None = Field(description="Ward or district, when the data has one.")


class AssistantGuide(BaseModel):
    id: str = Field(description="Knowledge section, e.g. day_pacing/meal-times.")
    title: str


class AssistantMeta(BaseModel):
    model: str | None
    retrieved: int = Field(description="Places passed to the model as context.")
    semantic: bool = Field(description="False when the places came from keyword search or none.")


class AssistantResponse(BaseModel):
    reply: str = Field(description="Plain text: short paragraphs, may use '- ' bullets.")
    places: list[AssistantPlace] = Field(
        description="Retrieved places the reply names, in the reply's order."
    )
    guides: list[AssistantGuide]
    meta: AssistantMeta
    # Optional so a client written before it still reads the response the same way.
    destination: Destination | None = Field(
        default=None,
        description=(
            "City the reply answered for and its places belong to: the request's destination, "
            "or another supported city the conversation asks about."
        ),
    )


class ModelAnswer(StrictModel):
    """What Gemini returns (its JSON Schema is the responseJsonSchema)."""

    reply: str
    place_ids: list[str]
    guide_ids: list[str]


RESPONSE_SCHEMA = ModelAnswer.model_json_schema()
