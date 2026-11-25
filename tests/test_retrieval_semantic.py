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
from app.services.retrieval import semantic
from app.services.retrieval.scoring import (
    ADDED_WEIGHTS,
    MATCH,
    SEMANTIC_FLOOR,
    Hit,
    match_score,
)
from app.services.retrieval.semantic import (
    QueryCache,
    document_hash,
    document_text,
    first_negation,
    negated,
)

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
        # dishes and double negatives are not negations
        ("bún chả", False),
        ("chả giò", False),
        ("chả cá Lã Vọng", False),
        ("địa điểm không thể bỏ lỡ", False),
        ("chỗ tránh nắng", False),
        # unaccented, abbreviated and English negations
        ("khong qua du lich", True),
        ("ko dong nguoi", True),
        ("khong gian xanh", False),
        ("don't like crowds", True),
        ("somewhere without tourists", True),
        ("never touristy", True),
        # "chẳng" written without accents, and English contractions without the apostrophe
        ("chang qua du lich", True),
        ("places that aren't touristy", True),
        ("I can't stand crowds", True),
        ("I cannot stand crowds", True),
        ("won’t like touristy spots", True),
        ("didn't want crowds", True),
        ("wasnt busy", True),
        ("shouldn't be crowded", True),
        # the accented words that fold to "chang" are not negations, nor is a must-see
        ("Chạng Vạng rooftop", False),
        ("quán chàng trai", False),
        ("a sunset you can't miss", False),
        # dusk typed without accents, and the Thai beer
        ("ngam chang vang tren cao", False),
        ("uống bia Chang", False),
        ("chang beer on a rooftop", False),
    ],
)
def test_negated_text_is_detected(text: str, expected: bool) -> None:
    assert negated(text) is expected


def test_the_first_negation_is_located_after_the_masked_phrases() -> None:
    assert first_negation("quán bar không ồn ào") == 2
    assert first_negation("không gian xanh, không ồn ào") == 3
    assert first_negation("don't want a bar") == 0
    assert first_negation("quán chill") is None


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


def test_closeness_spans_each_querys_own_hits() -> None:
    # Real similarities sit in a narrow band above the threshold; the closest hit gets 1.
    assert semantic.closeness({"a": 0.721, "b": 0.686, "c": 0.651}) == pytest.approx(
        {"a": 1.0, "b": 0.5, "c": 0.0}, abs=1e-3
    )
    assert semantic.closeness({"only": 0.7}) == {"only": 1.0}
    assert semantic.closeness({}) == {}


def test_a_semantic_hit_inside_a_group_section_still_orders_by_closeness() -> None:
    category = Hit("category", "prefer")
    near = match_score([category, Hit("semantic", "prefer", "x", closeness=1.0)])
    far = match_score([category, Hit("semantic", "prefer", "x", closeness=0.0)])
    assert near - far > ADDED_WEIGHTS["explore"]  # the seeded shuffle cannot undo it
    assert far > match_score([category])


def test_document_hash_changes_with_text_and_model() -> None:
    text = document_text("Quán A", "Quán cà phê yên tĩnh")
    assert text == "title: Quán A | text: Quán cà phê yên tĩnh"
    assert document_text("Quán A", None) == "title: Quán A | text: Quán A"
    assert document_hash("m1", text) != document_hash("m1", text + "!")
    assert document_hash("m1", text) != document_hash("m2", text)
    assert document_hash("models/m1", text) == document_hash("m1", text)


def test_closer_semantic_hits_score_higher() -> None:
    near = match_score([Hit("semantic", "prefer", "x", closeness=1.0)])
    far = match_score([Hit("semantic", "prefer", "x", closeness=0.0)])
    assert near > far
    assert far == pytest.approx(MATCH["semantic"] * SEMANTIC_FLOOR * 0.85)  # prefer = 0.85


def test_schema_vector_size_matches_the_embedding_size() -> None:
    schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text()
    assert f"embedding vector({EMBEDDING_DIMENSIONS})" in schema


def _embedded(requests: list, hits: dict) -> tuple[list[list[str]], list[str], frozenset[str]]:
    """Texts sent to the embedder by service._semantic, its warnings and the unsearched keys.
    The embedder fails after recording the call, so no database is needed (use fresh_cache)."""
    from app.services.retrieval.service import _semantic

    calls: list[list[str]] = []
    warnings: list[str] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        raise GeminiUpstreamError("stop after recording the call")

    job = _semantic(None, requests, {}, embed, hits, warnings)  # type: ignore[arg-type]
    _, unsearched = asyncio.run(job)
    return calls, warnings, unsearched


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic, "QUERY_CACHE", QueryCache())


def test_a_term_that_names_places_is_not_searched_by_meaning(fresh_cache: None) -> None:
    from app.services.retrieval.interests import InterestRequest

    pho = InterestRequest("phở", "prefer", "interest", text="pho")
    beer = InterestRequest("craft beer", "prefer", "interest", text="craft beer")
    quiet = InterestRequest("quán yên tĩnh", "prefer", "interest", text="quan yen tinh")
    named = {f"pho-{i}": "name" for i in range(semantic.SEMANTIC_NAME_HITS)}
    hits = {
        pho.key: {**named, "market": "text"},
        # Two stray name hits do not switch the search by meaning off.
        beer.key: {"yobe-craft-beer": "name", "craft-house": "name"},
        quiet.key: {"cafe": "text"},
    }
    calls, warnings, unsearched = _embedded([pho, beer, quiet], hits)
    assert calls == [[semantic.QUERY_FORMAT.format(t) for t in ("craft beer", "quán yên tĩnh")]]
    assert unsearched == frozenset() and not any("Mỗi lần chỉ tìm" in w for w in warnings)


def test_texts_past_the_cap_are_named_and_not_searched(fresh_cache: None) -> None:
    from app.services.retrieval.interests import InterestRequest

    texts = [f"chủ đề {i}" for i in range(semantic.MAX_QUERIES + 1)]
    requests = [
        InterestRequest(t, "prefer", "interest", text=f"chu de {i}") for i, t in enumerate(texts)
    ]
    calls, warnings, unsearched = _embedded(requests, {r.key: {} for r in requests})
    assert calls == [[semantic.QUERY_FORMAT.format(t) for t in texts[:-1]]]
    assert unsearched == frozenset({requests[-1].key})
    assert any(f"“{texts[-1]}”" in w and "theo ý nghĩa" in w for w in warnings), warnings
    assert not any("Không tìm thấy" in w for w in warnings)


def test_a_cached_text_evicted_while_waiting_for_the_embedder_is_still_returned() -> None:
    async def scenario() -> dict[str, list[float]]:
        cache = QueryCache(size=1)
        release = asyncio.Event()

        async def slow(texts: list[str]) -> list[list[float]]:
            await release.wait()
            return [[1.0] for _ in texts]

        async def fast(texts: list[str]) -> list[list[float]]:
            return [[2.0] for _ in texts]

        await cache.get_many(["old"], fast)
        waiting = asyncio.create_task(cache.get_many(["old", "new"], slow))
        await asyncio.sleep(0)  # A is waiting for its embedding of "new"
        await cache.get_many(["other"], fast)  # B evicts "old" (size 1)
        release.set()
        return await waiting

    assert asyncio.run(scenario()) == {"old": [2.0], "new": [1.0]}
