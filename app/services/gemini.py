"""Minimal Google AI Studio (Gemini API) client for structured JSON generation.

The request shape matches the journie-data benchmark adapter: instructions go in
``systemInstruction``, the user text in ``contents``, and the schema in ``responseJsonSchema``.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings


class GeminiError(Exception):
    """Base class for failures talking to the Gemini API."""


class GeminiTimeoutError(GeminiError):
    pass


class GeminiRateLimitError(GeminiError):
    def __init__(self, retry_after: str | None) -> None:
        super().__init__("Gemini quota or rate limit exceeded")
        self.retry_after = retry_after


class GeminiUpstreamError(GeminiError):
    """Network failure, non-2xx status, or a response without usable candidates."""


@dataclass(frozen=True, slots=True)
class Generation:
    text: str
    finish_reason: str | None
    resolved_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None


class GeminiClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, api_key: str) -> None:
        self._http = http
        self._settings = settings
        self._headers = {"x-goog-api-key": api_key}

    @property
    def model(self) -> str:
        return self._settings.gemini_model.removeprefix("models/")

    async def generate_json(
        self, system_prompt: str, user_text: str, response_schema: dict[str, Any]
    ) -> Generation:
        """Run one non-streaming generation constrained to ``response_schema``.

        Raises:
            GeminiTimeoutError: The request exceeded the configured timeout.
            GeminiRateLimitError: Gemini returned HTTP 429.
            GeminiUpstreamError: Any other transport, HTTP, or payload failure.
        """
        settings = self._settings
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": settings.gemini_temperature,
                "maxOutputTokens": settings.gemini_max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": response_schema,
            },
        }
        url = f"{settings.gemini_base_url.rstrip('/')}/models/{self.model}:generateContent"
        try:
            response = await self._http.post(
                url, headers=self._headers, json=body, timeout=settings.gemini_timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise GeminiTimeoutError("Gemini request timed out") from exc
        except httpx.RequestError as exc:
            # The message omits the URL and headers so the API key never reaches logs.
            raise GeminiUpstreamError(f"Gemini network error: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise GeminiRateLimitError(response.headers.get("retry-after"))
        if response.is_error:
            raise GeminiUpstreamError(f"Gemini returned HTTP {response.status_code}")
        try:
            return _parse_generation(response.json())
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise GeminiUpstreamError("Gemini returned an unexpected payload") from exc


def _parse_generation(payload: dict[str, Any]) -> Generation:
    block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
    candidate = payload["candidates"][0] if payload.get("candidates") else {}
    if not candidate and not block_reason:
        raise ValueError("Gemini returned no candidates or block reason")
    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(part["text"] for part in parts if "text" in part and not part.get("thought"))
    usage = payload.get("usageMetadata") or {}
    return Generation(
        text=text,
        finish_reason=candidate.get("finishReason") or block_reason,
        resolved_model=payload.get("modelVersion"),
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
        total_tokens=usage.get("totalTokenCount"),
        reasoning_tokens=usage.get("thoughtsTokenCount"),
    )


# Fixed: pois.embedding is vector(768) (db/schema.sql); changing it means re-embedding every
# place. 768 is one of Google's recommended sizes and gemini-embedding-2 normalizes it.
EMBEDDING_DIMENSIONS = 768
# shortcut: batchEmbedContents' item limit is not stated in the docs; 100 is the long-standing
# limit. Callers split larger lists.
EMBED_BATCH_LIMIT = 100


async def embed_texts(
    http: httpx.AsyncClient, settings: Settings, texts: list[str], keys: list[str]
) -> list[list[float]]:
    """One batchEmbedContents call for up to EMBED_BATCH_LIMIT texts. On HTTP 429 the next key
    is tried (separate free-tier quotas); the last 429 is raised.

    Raises:
        GeminiTimeoutError, GeminiRateLimitError, GeminiUpstreamError: as for generate_json.
    """
    if not texts:
        return []
    if len(texts) > EMBED_BATCH_LIMIT:
        raise ValueError(f"at most {EMBED_BATCH_LIMIT} texts per call, got {len(texts)}")
    if not keys:
        raise ValueError("no API key")
    model = settings.gemini_embedding_model.removeprefix("models/")
    url = f"{settings.gemini_base_url.rstrip('/')}/models/{model}:batchEmbedContents"
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": EMBEDDING_DIMENSIONS,
            }
            for text in texts
        ]
    }
    limited: GeminiRateLimitError | None = None
    for key in keys:
        try:
            response = await http.post(
                url,
                headers={"x-goog-api-key": key},
                json=body,
                timeout=settings.gemini_embedding_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise GeminiTimeoutError("Gemini embedding request timed out") from exc
        except httpx.RequestError as exc:
            raise GeminiUpstreamError(f"Gemini network error: {type(exc).__name__}") from exc
        if response.status_code == 429:
            limited = GeminiRateLimitError(response.headers.get("retry-after"))
            continue
        if response.is_error:
            raise GeminiUpstreamError(f"Gemini returned HTTP {response.status_code}")
        try:
            vectors = [item["values"] for item in response.json()["embeddings"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise GeminiUpstreamError("Gemini returned an unexpected embedding payload") from exc
        if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise GeminiUpstreamError("Gemini returned embeddings of the wrong count or size")
        return vectors
    assert limited is not None
    raise limited


async def embed_all(
    http: httpx.AsyncClient, settings: Settings, texts: list[str], keys: list[str]
) -> list[list[float]]:
    """embed_texts over any number of texts, EMBED_BATCH_LIMIT per call, in order."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_LIMIT):
        chunk = texts[start : start + EMBED_BATCH_LIMIT]
        vectors += await embed_texts(http, settings, chunk, keys)
    return vectors
