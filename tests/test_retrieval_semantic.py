"""Semantic search pieces that need no database: the embedding call, negation, the query cache,
document hashing and how a semantic hit is scored."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.services.gemini import (
    EMBED_BATCH_LIMIT,
    EMBEDDING_DIMENSIONS,
    GeminiRateLimitError,
    GeminiUpstreamError,
    embed_all,
    embed_texts,
)
from app.services.retrieval.scoring import MATCH, SEMANTIC_FLOOR, Hit, match_score
from app.services.retrieval.semantic import QueryCache, document_hash, document_text, negated

SETTINGS = Settings(_env_file=None)
VECTOR = [0.5] * EMBEDDING_DIMENSIONS


def call(handler, texts: list[str], keys: list[str], many: bool = False):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await (embed_all if many else embed_texts)(http, SETTINGS, texts, keys)

    return asyncio.run(run())


def ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"embeddings": [{"values": VECTOR} for _ in body["requests"]]})


def test_one_batch_call_with_the_task_text_and_size() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    assert call(handler, ["a", "b"], ["k1"]) == [VECTOR, VECTOR]
    [request] = seen
    assert request.url.path.endswith("/models/gemini-embedding-2:batchEmbedContents")
    assert request.headers["x-goog-api-key"] == "k1"
    first = json.loads(request.content)["requests"][0]
    assert first == {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": "a"}]},
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }


def test_a_rate_limited_key_hands_over_to_the_next() -> None:
    keys_used = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys_used.append(request.headers["x-goog-api-key"])
        return httpx.Response(429) if request.headers["x-goog-api-key"] == "k1" else ok(request)

    assert call(handler, ["a"], ["k1", "k2"]) == [VECTOR]
    assert keys_used == ["k1", "k2"]
    with pytest.raises(GeminiRateLimitError):
        call(lambda request: httpx.Response(429), ["a"], ["k1", "k2"])


@pytest.mark.parametrize(
    "answer",
    [
        {"embeddings": [{"values": [0.1] * 3}]},  # wrong size
        {"embeddings": []},  # wrong count
        {"unexpected": True},
    ],
)
def test_a_bad_payload_is_an_upstream_error(answer: dict) -> None:
    with pytest.raises(GeminiUpstreamError):
        call(lambda request: httpx.Response(200, json=answer), ["a"], ["k1"])


def test_long_lists_are_split_into_batches_in_order() -> None:
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(len(json.loads(request.content)["requests"]))
        return ok(request)

    texts = [str(i) for i in range(EMBED_BATCH_LIMIT + 20)]
    assert len(call(handler, texts, ["k1"], many=True)) == len(texts)
    assert sizes == [EMBED_BATCH_LIMIT, 20]
    with pytest.raises(ValueError, match="at most"):
        call(handler, texts, ["k1"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("không quá du lịch", True),
        ("tránh chỗ ồn ào đông người", True),
        ("Đừng đưa tôi tới chỗ đông", True),
        ("không gian xanh", False),
        ("không khí yên tĩnh", False),
        ("không gian xanh, không ồn ào", True),
        ("quán yên tĩnh để làm việc", False),
    ],
)
def test_negated_text_is_detected(text: str, expected: bool) -> None:
    assert negated(text) is expected


def test_the_cache_embeds_only_new_texts_and_stays_bounded() -> None:
    calls = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(t))] for t in texts]

    cache = QueryCache(size=2)
    first = asyncio.run(cache.get_many(["a", "bb"], embed))
    second = asyncio.run(cache.get_many(["bb", "ccc", "ccc"], embed))
    assert first == {"a": [1.0], "bb": [2.0]} and second == {"bb": [2.0], "ccc": [3.0]}
    assert calls == [["a", "bb"], ["ccc"]]  # "bb" was cached, "ccc" asked once
    asyncio.run(cache.get_many(["a"], embed))  # "a" was evicted (size 2)
    assert calls[-1] == ["a"]


def test_document_hash_changes_with_text_and_model() -> None:
    text = document_text("Quán A", "Quán cà phê yên tĩnh")
    assert text == "title: Quán A | text: Quán cà phê yên tĩnh"
    assert document_text("Quán A", None) == "title: Quán A | text: Quán A"
    assert document_hash("m1", text) != document_hash("m1", text + "!")
    assert document_hash("m1", text) != document_hash("m2", text)


def test_closer_semantic_hits_score_higher() -> None:
    near = match_score([Hit("semantic", "prefer", "x", closeness=1.0)])
    far = match_score([Hit("semantic", "prefer", "x", closeness=0.0)])
    assert near > far
    assert far == pytest.approx(MATCH["semantic"] * SEMANTIC_FLOOR * 0.85)  # prefer = 0.85


def test_schema_vector_size_matches_the_embedding_size() -> None:
    schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text()
    assert f"embedding vector({EMBEDDING_DIMENSIONS})" in schema
