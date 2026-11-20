"""Semantic search for free-text interests: the traveller's words against embedded place
descriptions (Gemini embeddings, pgvector).

Only requests that matched no taxonomy group and no keyword (InterestRequest.text) are searched
this way; "cafe" or "phở" keep their structured and full-text paths. Text with a negation is
skipped: an embedding of "không quá du lịch" sits next to "du lịch", so it would find exactly
the places the traveller wants to avoid.
"""

import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from psycopg import AsyncConnection

from app.services.gemini import EMBEDDING_DIMENSIONS
from app.services.retrieval.candidates import ELIGIBLE, SUGGESTABLE
from app.services.retrieval.text import fold_case

# gemini-embedding-2 takes no task_type: the task goes in the text (asymmetric retrieval format).
QUERY_FORMAT = "task: search result | query: {}"
DOCUMENT_FORMAT = "title: {} | text: {}"
# Picked on 12 probes taken from /chat output: 0.65 keeps every place wish (top hits 0.655-0.771)
# and drops 2 of 4 non-place phrases ("đi một mình" 0.648). Neither this nor a per-query z-score
# separates them all: "cuối tuần" still matches a bar named Wknd (0.742).
SIMILARITY_MIN = 0.65
TOP_K = 30  # semantic hits kept per request, before sectioning (a page shows 6)
QUERY_CACHE_SIZE = 512  # per process; a miss only costs one embedding call

NEGATIONS = frozenset({"không", "chẳng", "chả", "đừng", "tránh", "chớ", "no", "not", "avoid"})
# Words that start with a negation word but are not negations ("không gian xanh" = green space).
NOT_NEGATIONS = frozenset({"không gian", "không khí", "không trung", "hàng không"})

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
    """Changes when the text, the model or the vector size changes: the row is re-embedded."""
    return hashlib.sha256(f"{model}|{EMBEDDING_DIMENSIONS}|{text}".encode()).hexdigest()


def negated(text: str) -> bool:
    folded = f" {fold_case(text)} "
    for phrase in NOT_NEGATIONS:
        folded = folded.replace(f" {phrase} ", " ")
    return any(word in NEGATIONS for word in folded.split())


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


class QueryCache:
    """Bounded LRU of query embeddings, so 'xem thêm' pages do not embed the same words again.
    Per process: another instance just embeds once more."""

    def __init__(self, size: int = QUERY_CACHE_SIZE) -> None:
        self._size = size
        self._items: OrderedDict[str, list[float]] = OrderedDict()

    async def get_many(self, texts: list[str], embed: Embed) -> dict[str, list[float]]:
        missing = [text for text in dict.fromkeys(texts) if text not in self._items]
        if missing:
            for text, vector in zip(missing, await embed(missing), strict=True):
                self._items[text] = vector
        found = {}
        for text in texts:
            self._items.move_to_end(text)
            found[text] = self._items[text]
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
            {**params, "vector": vector_literal(vector), "k": TOP_K, "min": SIMILARITY_MIN},
        )
        return {poi_id: float(similarity) for poi_id, similarity in await cur.fetchall()}
