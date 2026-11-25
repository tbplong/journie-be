"""Small knowledge corpus for the assistant: markdown files in ``knowledge/``, one chunk per
``## heading``.

A heading reads "Vietnamese title | English title". Chunks are embedded lazily, once per process,
in one batch call (no table: the corpus is a few dozen chunks), and ranked per question by cosine
similarity with the question's embedding. Without embeddings, a word-overlap score ranks them.
"""

import asyncio
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from app.services.gemini import GeminiError
from app.services.retrieval.semantic import DOCUMENT_FORMAT, Embed
from app.services.retrieval.text import norm

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).with_name("knowledge")
TOP_K = 3
# After a failed embedding call the chunks are not embedded again for this long: each try costs
# a unit of the free-tier quota.
RETRY_AFTER_SECONDS = 60.0

_HEADING = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str  # "<file stem>/<slug of the English title>"
    title_vi: str
    title_en: str
    text: str
    topic: str = ""  # the file's "# title"

    @property
    def heading(self) -> str:
        return f"{self.topic}: {self.title_vi} / {self.title_en}".removeprefix(": ")

    def title(self, language: str) -> str:
        return self.title_en if language == "en" else self.title_vi

    @property
    def document(self) -> str:
        return DOCUMENT_FORMAT.format(self.heading, self.text)


def slug(text: str) -> str:
    return "-".join(norm(text).split())


def parse_chunks(stem: str, markdown: str) -> list[Chunk]:
    """One chunk per ``## heading``; the text before the first one is dropped except the file's
    ``# title``, which every chunk keeps as its topic."""
    parts = _HEADING.split(markdown)
    topic = next((line[2:].strip() for line in parts[0].splitlines() if line.startswith("# ")), "")
    chunks = []
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        title_vi, _, title_en = (part.strip() for part in heading.partition("|"))
        title_en = title_en or title_vi
        chunks.append(Chunk(f"{stem}/{slug(title_en)}", title_vi, title_en, body.strip(), topic))
    return chunks


def load_chunks(directory: Path = KNOWLEDGE_DIR) -> list[Chunk]:
    chunks = []
    for path in sorted(directory.glob("*.md")):
        chunks += parse_chunks(path.stem, path.read_text(encoding="utf-8"))
    ids = [chunk.id for chunk in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate knowledge chunk ids")
    return chunks


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    size = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / size if size else 0.0


def _words(text: str) -> set[str]:
    """Folded words and adjacent pairs: Vietnamese words are often two syllables ("bảo tàng")."""
    words = norm(text).split()
    return {word for word in words if len(word) > 1} | {f"{a} {b}" for a, b in pairwise(words)}


class Knowledge:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._words = [_words(f"{c.heading} {c.text}") for c in chunks]
        self._title_words = [_words(f"{c.title_vi} {c.title_en}") for c in chunks]
        # Inverse document frequency: a word every chunk has ("và", "the") scores nothing.
        counts = Counter(word for words in self._words for word in words)
        self._idf = {word: math.log(len(chunks) / count) for word, count in counts.items()}
        self._vectors: list[list[float]] | None = None
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()

    async def vectors(self, embed: Embed) -> list[list[float]] | None:
        """The chunks' embeddings, or None while embedding is unavailable."""
        if self._vectors is not None:
            return self._vectors
        async with self._lock:
            if self._vectors is not None:
                return self._vectors
            now = time.monotonic()
            if self._failed_at is not None and now - self._failed_at < RETRY_AFTER_SECONDS:
                return None
            try:
                vectors = await embed([chunk.document for chunk in self.chunks])
                if len(vectors) != len(self.chunks):
                    raise ValueError("wrong number of chunk embeddings")
            except (GeminiError, ValueError) as exc:  # must not fail the answer
                logger.warning("Knowledge chunks not embedded: %s", type(exc).__name__)
                self._failed_at = now
                return None
            self._vectors = vectors
            return vectors

    async def search(
        self,
        question: str,
        query_vector: Sequence[float] | None,
        embed: Embed | None,
        k: int = TOP_K,
    ) -> list[Chunk]:
        """The k chunks closest to the question: by embedding when both the question vector and
        the chunk vectors are available, else by shared words (chunks sharing none are left out)."""
        vectors = None
        if query_vector is not None and embed is not None:
            vectors = await self.vectors(embed)
        if vectors is not None and query_vector is not None:
            scored = [
                (cosine(query_vector, vector), chunk)
                for vector, chunk in zip(vectors, self.chunks, strict=True)
            ]
        else:
            asked = _words(question)
            # A word in the heading counts twice.
            scored = [
                (sum(self._idf[word] for word in (asked & words) | (asked & title)), chunk)
                for words, title, chunk in zip(
                    self._words, self._title_words, self.chunks, strict=True
                )
            ]
            scored = [(score, chunk) for score, chunk in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:k]]


KNOWLEDGE = Knowledge(load_chunks())
