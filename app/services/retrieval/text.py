"""Text normalization and accent-aware lexemes for retrieval.

Two forms of every word:

* ``norm()`` folds accents (đ -> d). Folding makes many Vietnamese words collide (phở/phố,
  bò/bộ, tấm/tâm; ~7% of distinct words in the sample), so it is only a fallback.
* ``fold_case()`` keeps diacritics and only drops case and punctuation.

Accent rule, applied per word (no word lists): a query word with diacritics matches a document
word that is identical, or that has no diacritics and equals the folded query word ("phở" ->
"phở", "pho"; never "phố"). A query word without diacritics carries no accent information, so
it matches any document word with the same folded form.

``lexeme_vector`` / ``lexeme_query`` encode that rule for Postgres full-text search: each
document word is stored at its position with weight A (as written) and, when folding changes
it, weight B (folded). Importer and query side share the tokenizer, so they cannot drift.
"""

import re
import unicodedata
from collections.abc import Iterable

_NON_WORD = re.compile(r"[\W_]+")

# Postgres keeps tsvector positions <= 16383; later words share the last position, which only
# weakens phrase matching inside unusually long documents.
MAX_POSITION = 16383
# Position gap between separate texts (name, alias, description) so a phrase never spans two.
TEXT_GAP = 10


def norm(text: str | None) -> str:
    """Lowercase, strip accents (đ -> d), turn punctuation and ``_`` into spaces, collapse."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_WORD.sub(" ", stripped).strip()


def fold_case(text: str | None) -> str:
    """Lowercase (NFC), punctuation and ``_`` to spaces, collapse; diacritics are kept."""
    if not text:
        return ""
    return _NON_WORD.sub(" ", unicodedata.normalize("NFC", text.casefold())).strip()


def has_diacritics(word: str) -> bool:
    return norm(word) != word


def _quote(lexeme: str) -> str:
    """tsvector/tsquery literal quoting."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def lexeme_vector(texts: Iterable[str | None]) -> str:
    """tsvector literal: every word at weight A, plus its folded form at weight B if different.

    Empty input gives ``''`` (an empty tsvector).
    """
    positions: dict[str, list[str]] = {}
    position = 0
    for text in texts:
        words = fold_case(text).split()
        if not words:
            continue
        for word in words:
            position = min(position + 1, MAX_POSITION)
            positions.setdefault(word, []).append(f"{position}A")
            folded = norm(word)
            if folded != word:
                positions.setdefault(folded, []).append(f"{position}B")
        position = min(position + TEXT_GAP, MAX_POSITION)
    return " ".join(f"{_quote(lexeme)}:{','.join(spots)}" for lexeme, spots in positions.items())


def lexeme_query(text: str | None) -> str | None:
    """tsquery literal matching the words of ``text`` as a phrase under the accent rule.

    None when the text has no words.
    """
    words = fold_case(text).split()
    if not words:
        return None
    parts = []
    for word in words:
        if has_diacritics(word):
            parts.append(f"({_quote(word)}:A | {_quote(norm(word))}:A)")
        else:
            parts.append(f"{_quote(word)}:AB")
    return " <-> ".join(parts)
