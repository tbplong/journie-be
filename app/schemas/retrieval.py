"""POI retrieval request/response models (POST /retrieval)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.extraction import Extraction

NameField = Literal["must_visit", "preferred_places", "avoid"]

# Each name and interest costs a query (service.retrieve), and the client sends the extraction,
# so the lists are bounded here; Extraction itself is the Gemini schema and must not change.
MAX_LIST_ITEMS = 20
MAX_CONSTRAINTS = 50
# Each entry is matched against every place in SQL; a real name or interest is far shorter.
MAX_ENTRY_CHARS = 200


class RetrievalRequest(BaseModel):
    extraction: Extraction = Field(description="The `extraction` returned by POST /chat.")
    selected_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="POIs the traveller already picked; nearby places rank higher.",
    )
    section: str | None = Field(
        default=None, description="Return only this section key (for 'xem thêm' paging)."
    )
    offset: int = Field(default=0, ge=0, description="Items to skip in each returned section.")
    limit: int = Field(default=6, ge=1, le=20, description="Items per returned section.")
    seed: int | None = Field(
        default=None,
        ge=0,
        lt=2**32,
        description=(
            "Orders equally relevant places. Default: derived from the extraction, so the same "
            "request (and its pages) keeps one order; send a new seed for other suggestions."
        ),
    )

    @model_validator(mode="after")
    def bounded_lists(self) -> "RetrievalRequest":
        limits = {
            "interests": MAX_LIST_ITEMS,
            "must_visit": MAX_LIST_ITEMS,
            "avoid": MAX_LIST_ITEMS,
            "preferred_places": MAX_LIST_ITEMS,
            "hard_constraints": MAX_CONSTRAINTS,
            "soft_constraints": MAX_CONSTRAINTS,
        }
        for name, limit in limits.items():
            entries = getattr(self.extraction, name)
            if len(entries) > limit:
                raise ValueError(f"extraction.{name} has more than {limit} entries")
            texts = [getattr(entry, "value", entry) for entry in entries]
            if any(len(text) > MAX_ENTRY_CHARS for text in texts):
                raise ValueError(
                    f"extraction.{name} has an entry longer than {MAX_ENTRY_CHARS} characters"
                )
        return self


class PoiCard(BaseModel):
    id: str
    name: str
    kind: str
    category: str | None = Field(description="Taxonomy value top.sub, e.g. food.cafe.")
    ward: str | None
    address: str | None
    lat: float | None
    lon: float | None
    score: float
    score_parts: dict[str, float]
    reasons: list[str] = Field(description="Built only from stored column values.")
    flags: list[str]
    price_raw: str | None
    opening_hours_raw: str | None
    website: str | None
    phone: str | None


class Disambiguation(BaseModel):
    query: str
    field: NameField
    candidates: list[PoiCard]


class NotFound(BaseModel):
    query: str
    field: NameField


class Section(BaseModel):
    key: str
    label: str
    required: bool = Field(description="The traveller asked for this as mandatory.")
    total_matches: int
    items: list[PoiCard]
    matched_keywords: list[str] = Field(
        default_factory=list, description="Non-standard interests merged into this section."
    )
    note: str | None = None


class RetrievalMeta(BaseModel):
    retrieval_version: str
    interest_map_version: str
    keyword_map_version: str
    data_version: str | None
    latency_ms: int


class RetrievalResponse(BaseModel):
    status: Literal["ok", "unsupported_destination", "empty_extraction"]
    pinned: list[PoiCard] = Field(description="Resolved must_visit places, always shown first.")
    disambiguation: list[Disambiguation]
    not_found: list[NotFound]
    sections: list[Section]
    warnings: list[str]
    meta: RetrievalMeta
