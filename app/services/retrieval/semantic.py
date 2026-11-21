"""Semantic search for free-text interests: the traveller's words against embedded place
descriptions (Gemini embeddings, pgvector).

Only requests that matched no taxonomy group and no keyword (InterestRequest.text) and whose
words match no place name are searched this way; "cafe" or "phở" keep their structured and
full-text paths (service._semantic). Text with a negation is skipped: an embedding of "không quá du lịch" sits next to "du lịch", so it would find exactly
the places the traveller wants to avoid.
"""

import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from psycopg import AsyncConnection

from app.services.gemini import EMBEDDING_DIMENSIONS
from app.services.retrieval.candidates import ELIGIBLE, SUGGESTABLE
from app.services.retrieval.sections import SEMANTIC_KEEP
from app.services.retrieval.text import fold_case, norm

# gemini-embedding-2 takes no task_type: the task goes in the text (asymmetric retrieval format).
QUERY_FORMAT = "task: search result | query: {}"
DOCUMENT_FORMAT = "title: {} | text: {}"
# Picked on 12 probes taken from /chat output: 0.65 keeps every place wish (top hits 0.655-0.771)
# and drops 2 of 4 non-place phrases ("đi một mình" 0.648). Neither this nor a per-query z-score
# separates them all: "cuối tuần" still matches a bar named Wknd (0.742).
SIMILARITY_MIN = 0.65
# Nearest places fetched per request: sections keeps the SEMANTIC_KEEP closest after the hours,
# avoid and already-picked filters (<= 70 picked and pinned places per request).
FETCH_K = SEMANTIC_KEEP * 4
QUERY_CACHE_SIZE = 512  # per process; a miss only costs one embedding call
# Free-text interests searched by meaning per request; each uncached one costs a unit of the
# embedding quota, and the request is unauthenticated. The rest are matched by words only.
MAX_QUERIES = 5

# "chả" is left out: it is far more often a dish (bún chả, chả giò) than a negation. "khong" and
# "ko" are the unaccented spellings no place wish uses (folding keeps "khổng" as is).
NEGATIONS = frozenset({
    "không", "chẳng", "đừng", "tránh", "chớ", "khong", "ko",
    "no", "not", "avoid", "without", "never", "dont", "doesnt", "isnt", "nothing",
})
# Phrases holding a negation word that are not negations: "không gian xanh" = green space,
# "không thể bỏ lỡ" = must-see, "tránh nắng" = shelter from the sun.
NOT_NEGATIONS = frozenset({
    "không gian", "không khí", "không trung", "hàng không",
    "không thể bỏ lỡ", "không thể bỏ qua", "không nên bỏ lỡ", "không nên bỏ qua",
    "tránh nắng", "tránh nóng", "tránh mưa",
})

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

SEMANTIC_SQL = f"""
    SELECT id, similarity FROM (
        SELECT id, 1 - (embedding <=> %(vector)s::vector) AS similarity
        FROM pois
        WHERE {ELIGIBLE} AND {SUGGESTABLE} AND embedding IS NOT NULL
        ORDER BY embedding <=> %(vector)s::vector
        LIMIT %(k)s
    ) AS nearest
    WHERE similarity >= %(min)s
"""


def document_text(name: str, description: str | None) -> str:
    return DOCUMENT_FORMAT.format(name, description or name)


def document_hash(model: str, text: str) -> str:
    """Changes when the text, the model or the vector size changes: the row is re-embedded.
    "models/x" and "x" name one model (gemini.embed_texts calls it either way)."""
    model = model.removeprefix("models/")
    return hashlib.sha256(f"{model}|{EMBEDDING_DIMENSIONS}|{text}".encode()).hexdigest()


def negated(text: str) -> bool:
    # "don't" -> "dont" before punctuation turns the apostrophe into a word break.
    folded = f" {fold_case(text.replace('’', '').replace("'", ''))} "
    # Accented phrases first, then their unaccented spelling (matches only unaccented words).
    for phrase in (*NOT_NEGATIONS, *map(norm, NOT_NEGATIONS)):
        folded = folded.replace(f" {phrase} ", " ")
    return any(word in NEGATIONS for word in folded.split())


def closeness(found: dict[str, float]) -> dict[str, float]:
    """Similarity -> 0 at SIMILARITY_MIN, 1 at this query's closest place. One query's hits sit
    in a narrow band (0.65-0.77 seen), which a fixed 0.65-1 scale flattens below the seeded
    shuffle, so semantic sections would not be ordered by closeness."""
    if not found:
        return {}
    span = max(found.values()) - SIMILARITY_MIN
    return {i: (sim - SIMILARITY_MIN) / span if span > 0 else 1.0 for i, sim in found.items()}


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


class QueryCache:
    """Bounded LRU of query embeddings, so 'xem thêm' pages do not embed the same words again.
    Per process: another instance just embeds once more."""

    def __init__(self, size: int = QUERY_CACHE_SIZE) -> None:
        self._size = size
        self._items: OrderedDict[str, list[float]] = OrderedDict()

    async def get_many(self, texts: list[str], embed: Embed) -> dict[str, list[float]]:
        # Copy the cached vectors before awaiting: another request may evict them meanwhile.
        found = {text: self._items[text] for text in texts if text in self._items}
        missing = [text for text in dict.fromkeys(texts) if text not in found]
        if missing:
            found.update(zip(missing, await embed(missing), strict=True))
        for text, vector in found.items():
            self._items[text] = vector
            self._items.move_to_end(text)
        while len(self._items) > self._size:
            self._items.popitem(last=False)
        return found


QUERY_CACHE = QueryCache()


async def semantic_hits(
    conn: AsyncConnection, vector: Sequence[float], params: dict[str, Any]
) -> dict[str, float]:
    """Eligible places whose description is close to the query: id -> cosine similarity."""
    async with conn.cursor() as cur:
        await cur.execute(
            SEMANTIC_SQL,
            {**params, "vector": vector_literal(vector), "k": FETCH_K, "min": SIMILARITY_MIN},
        )
        return {poi_id: float(similarity) for poi_id, similarity in await cur.fetchall()}
