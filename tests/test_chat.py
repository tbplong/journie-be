import json
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.chat import get_gemini_client
from app.core.config import Settings, get_settings
from app.main import app
from app.services.extraction.prompt import build_system_prompt
from app.services.gemini import GeminiClient

Handler = Callable[[httpx.Request], httpx.Response]

EMPTY: dict[str, Any] = {
    "destination": None,
    "start_date": None,
    "end_date": None,
    "duration_days": None,
    "budget_vnd": None,
    "budget_scope": None,
    "available_start": None,
    "available_end": None,
    "interests": [],
    "must_visit": [],
    "avoid": [],
    "preferred_places": [],
    "hard_constraints": [],
    "soft_constraints": [],
    "missing_fields": ["destination", "travel_dates_or_duration", "budget", "available_time"],
    "possible_conflicts": [],
    "needs_clarification": False,
}

DA_LAT = {
    **EMPTY,
    "destination": "Dalat",
    "duration_days": 3,
    "budget_vnd": 3000000,
    "interests": ["cà phê"],
    "hard_constraints": [
        {"field": "destination", "operator": "eq", "value": "Dalat"},
        {"field": "duration_days", "operator": "eq", "value": "3"},
        {"field": "budget_vnd", "operator": "max", "value": "3000000"},
    ],
    "soft_constraints": [{"field": "interests", "operator": "prefer", "value": "cà phê"}],
    "missing_fields": [],
}


def gemini_payload(text: str, finish_reason: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}],
        "usageMetadata": {
            "promptTokenCount": 2600,
            "candidatesTokenCount": 180,
            "totalTokenCount": 2780,
        },
        "modelVersion": "gemini-3.5-flash-lite-001",
    }


@pytest.fixture
def use_gemini() -> Iterator[Callable[[Handler], list[httpx.Request]]]:
    """Route Gemini calls to ``handler``; returns the list of captured requests."""
    settings = Settings(gemini_api_key="test-key", _env_file=None)  # type: ignore[call-arg]

    def install(handler: Handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(record))
        app.dependency_overrides[get_gemini_client] = lambda: GeminiClient(
            http, settings, "test-key"
        )
        return seen

    yield install
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_extraction_success_applies_repair_v3(
    client: TestClient, use_gemini: Callable[[Handler], list[httpx.Request]]
) -> None:
    seen = use_gemini(lambda _: httpx.Response(200, json=gemini_payload(json.dumps(DA_LAT))))

    response = client.post(
        "/chat",
        json={"text": "Đi Dalat 3 ngày, tối đa 3tr, thích cà phê", "reference_date": "2026-11-01"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["extraction"]["destination"] == "Da Lat"
    assert body["extraction"]["interests"] == ["cafe"]
    assert "canonical_name" in body["repairs"]
    assert body["issues"] == []
    assert body["meta"]["prompt_version"] == "semantic-v3"
    assert body["meta"]["processing_version"] == "repair-v3"
    assert body["meta"]["reference_date"] == "2026-11-01"
    assert body["meta"]["usage"]["input_tokens"] == 2600

    sent = json.loads(seen[0].content)
    assert seen[0].headers["x-goog-api-key"] == "test-key"
    assert seen[0].url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert sent["systemInstruction"]["parts"][0]["text"] == build_system_prompt(date(2026, 11, 1))
    assert sent["contents"][0]["parts"][0]["text"] == "Đi Dalat 3 ngày, tối đa 3tr, thích cà phê"
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    assert sent["generationConfig"]["responseJsonSchema"]["title"] == "Extraction"


def test_reference_date_defaults_to_today_in_vietnam(
    client: TestClient,
    use_gemini: Callable[[Handler], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.extraction.service.today", lambda: date(2027, 1, 15))
    seen = use_gemini(lambda _: httpx.Response(200, json=gemini_payload(json.dumps(EMPTY))))

    response = client.post("/chat", json={"text": "write me a poem"})

    assert response.json()["meta"]["reference_date"] == "2027-01-15"
    prompt = json.loads(seen[0].content)["systemInstruction"]["parts"][0]["text"]
    assert "Reference date: 2027-01-15." in prompt


@pytest.mark.parametrize(
    ("text", "finish_reason"),
    [
        ("", "SAFETY"),
        ('{"destination": "Hue"', "MAX_TOKENS"),
        (json.dumps({**EMPTY, "duration_days": "3"}), "STOP"),
        (json.dumps({**EMPTY, "extra": 1}), "STOP"),
    ],
)
def test_invalid_model_output_is_502(
    client: TestClient,
    use_gemini: Callable[[Handler], list[httpx.Request]],
    text: str,
    finish_reason: str,
) -> None:
    use_gemini(lambda _: httpx.Response(200, json=gemini_payload(text, finish_reason)))

    response = client.post("/chat", json={"text": "Hue 2 ngày"})

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "/problems/invalid-llm-output"


def test_blocked_prompt_without_candidates_is_invalid_output(
    client: TestClient, use_gemini: Callable[[Handler], list[httpx.Request]]
) -> None:
    # A safety block comes back as promptFeedback only, with no candidates at all.
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {}}
    use_gemini(lambda _: httpx.Response(200, json=blocked))

    response = client.post("/chat", json={"text": "Hue 2 ngày"})

    assert response.status_code == 502
    assert response.json()["type"] == "/problems/invalid-llm-output"
    assert "finish_reason=SAFETY" in response.json()["detail"]


def test_rate_limit_is_503_with_retry_after(
    client: TestClient, use_gemini: Callable[[Handler], list[httpx.Request]]
) -> None:
    use_gemini(lambda _: httpx.Response(429, headers={"retry-after": "12"}))

    response = client.post("/chat", json={"text": "Hue 2 ngày"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "12"
    assert response.json()["type"] == "/problems/llm-rate-limited"


def test_timeout_is_504(
    client: TestClient, use_gemini: Callable[[Handler], list[httpx.Request]]
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    use_gemini(timeout)

    response = client.post("/chat", json={"text": "Hue 2 ngày"})

    assert response.status_code == 504
    assert response.json()["type"] == "/problems/llm-timeout"


@pytest.mark.parametrize("status_code", [400, 500, 503])
def test_upstream_http_error_is_502(
    client: TestClient,
    use_gemini: Callable[[Handler], list[httpx.Request]],
    status_code: int,
) -> None:
    use_gemini(lambda _: httpx.Response(status_code, json={"error": {"message": "boom"}}))

    response = client.post("/chat", json={"text": "Hue 2 ngày"})

    assert response.status_code == 502
    assert response.json()["detail"] == f"Gemini returned HTTP {status_code}"


def test_missing_api_key_is_503(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        gemini_api_key=None,
        _env_file=None,  # type: ignore[call-arg]
    )
    try:
        response = client.post("/chat", json={"text": "Hue 2 ngày"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["type"] == "/problems/llm-not-configured"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 4001},
        {"text": "Hue", "reference_date": "x"},
    ],
)
def test_request_validation_is_422(
    client: TestClient,
    use_gemini: Callable[[Handler], list[httpx.Request]],
    payload: dict[str, Any],
) -> None:
    seen = use_gemini(lambda _: httpx.Response(500))

    assert client.post("/chat", json=payload).status_code == 422
    assert seen == []
