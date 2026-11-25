"""Semantic search for free-text interests: the traveller's words against embedded place
descriptions (Gemini embeddings, pgvector).

Only requests that matched no taxonomy group and no keyword (InterestRequest.text) and whose
words do not already name SEMANTIC_NAME_HITS places are searched this way; "cafe" or "phở"
keep their structured and full-text paths (service._semantic). Text with a negation is skipped:
an embedding of "không quá du lịch" sits next to "du lịch", so it would find exactly the places
the traveller wants to avoid.
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
# First picked on 12 probes from /chat output (place wishes' top hits 0.655-0.771; 2 of 4
# non-place phrases dropped). Re-measured on 2026-11-24 after the descriptions were rewritten
# and re-embedded: eval/run_eval.py --similarities on the 76 eval cases (4 cities) plus 25
# probes, the 8 original ones that were written down among them. The five original place wishes
# now top at 0.684-0.759; "đi một mình" (0.648) still drops, "đi cùng trẻ em" (0.716) does not.
# Eval cases passed: 0.60 67, 0.62-0.63 68, 0.64-0.65 69, 0.655-0.66 67, 0.67-0.68 69, 0.70 65.
# 0.65 is the highest value that keeps 3 rooftop bars for "ngắm hoàng hôn" in Đà Nẵng (the
# third at 0.652) and the malls for "trời mưa đi đâu trong nhà"; 0.64-0.645 pass nothing more
# and let in karaoke rooms for "nhạc sống" and wrong places for "sống ảo chụp ảnh". 0.67-0.68
# win two Hà Nội drinking cases only by losing "ngắm hoàng hôn" and either "chỗ cho trẻ em" or
# the Huế bars. No value keeps non-place phrases out: "đi cùng bạn bè" (0.712) and "đi cùng gia
# đình" (0.707) match places named or described for friends and families (King Beer 85, Karaoke
# Gia Đình), above the best hit of "yên tĩnh" (0.689) or "lịch sử chiến tranh" (0.684); time
# phrases ("cuối tuần": Wknd at 0.742) are dropped as ignored_wishes in keyword_map instead. A
# second value by source does not help: both kinds arrive as `other` atoms. The drinking cases
# that moved with the value were a section-placement bug (a group's companion text taking the
# group's bars), fixed in sections.py; re-run on 92 cases after that fix: 0.60 85, 0.62-0.63 86,
# 0.64-0.65 88, 0.655 86, 0.66-0.67 85, 0.68 84, 0.70 83.
# Raised to 0.651 the same day after 49 bar descriptions lost the generic "rượu, bia hoặc
# cocktail" sentence and were re-embedded: Home Bar Là Nhà went from 0.643 to 0.6504 for "trời
# mưa đi đâu trong nhà" and entered that section (rain-hcm). 0.6505-0.652 pass 89/92 with the
# same P@5 and nDCG@5 as 0.65; 0.653 loses the third rooftop bar of "ngắm hoàng hôn" in Đà Nẵng.
# Outside the eval, "chỗ vui chơi cho trẻ em" in Huế loses its only hit (a museum at 0.6504).
SIMILARITY_MIN = 0.651
# Nearest places fetched per request: sections keeps the SEMANTIC_KEEP closest after the hours,
# avoid and already-picked filters (<= 70 picked and pinned places per request).
FETCH_K = SEMANTIC_KEEP * 4
QUERY_CACHE_SIZE = 512  # per process; a miss only costs one embedding call
# Free-text interests searched by meaning per request; each uncached one costs a unit of the
# embedding quota, and the request is unauthenticated. The rest are matched by words only.
MAX_QUERIES = 5
# A text whose words already name this many places ("phở", "bánh mì") is not searched by
# meaning: embeddings ignore the accent rule, so "phở" would find "Đông Phố". One or two stray
# name hits must not switch it off: "rượu" names one restaurant (Hầm Rượu), "craft beer" two.
# Re-checked on the 2026-11-24 eval (92 cases): 1 loses "rooftop bar" and "craft beer" in
# TP.HCM, 3 and 8 change no case.
SEMANTIC_NAME_HITS = 5

# "chả" is left out: it is far more often a dish (bún chả, chả giò) than a negation. "khong",
# "chang" and "ko" are the unaccented spellings no place wish uses (folding keeps "khổng" and
# "chạng" as is). English contractions lose their apostrophe first (negated()).
NEGATIONS = frozenset({
    "không", "chẳng", "đừng", "tránh", "chớ", "khong", "chang", "ko",
    "no", "not", "avoid", "without", "never", "nothing", "cannot",
    "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent", "cant", "couldnt", "wont",
    "wouldnt", "shouldnt", "hasnt", "havent",
})
# Phrases holding a negation word that are not negations: "không gian xanh" = green space,
# "không thể bỏ lỡ" = must-see, "tránh nắng" = shelter from the sun, "chạng vạng" = dusk (also
# typed "chang vang"), Chang = a Thai beer.
NOT_NEGATIONS = frozenset({
    "không gian", "không khí", "không trung", "hàng không",
    "không thể bỏ lỡ", "không thể bỏ qua", "không nên bỏ lỡ", "không nên bỏ qua",
    "tránh nắng", "tránh nóng", "tránh mưa", "cant miss", "cannot miss",
    "chạng vạng", "bia chang", "chang beer",
})
# Accented phrases match only words as written; their unaccented spelling only unaccented words.
_NOT_NEGATION_WORDS = tuple(
    tuple(phrase.split()) for phrase in sorted({*NOT_NEGATIONS, *map(norm, NOT_NEGATIONS)})
)

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


def words_of(text: str) -> list[str]:
    """fold_case words; "don't" -> "dont" first (punctuation would split it in two)."""
    return fold_case(text.replace("’", "").replace("'", "")).split()


def first_negation(text: str) -> int | None:
    """Index in words_of(text) of the first negation word outside NOT_NEGATIONS, if any."""
    words = words_of(text)
    for phrase in _NOT_NEGATION_WORDS:
        size = len(phrase)
        for start in range(len(words) - size + 1):
            if tuple(words[start : start + size]) == phrase:
                words[start : start + size] = [""] * size
    return next((n for n, word in enumerate(words) if word in NEGATIONS), None)


def negated(text: str) -> bool:
    return first_negation(text) is not None


def closeness(found: dict[str, float]) -> dict[str, float]:
    """Similarity -> 0 at SIMILARITY_MIN, 1 at this query's closest place. One query's hits sit
    in a narrow band (0.65-0.79 seen), which a fixed 0.65-1 scale flattens below the seeded
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
