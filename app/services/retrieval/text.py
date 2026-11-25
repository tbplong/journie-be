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

Strict variant (``lexeme_query(text, strict=True)``), used on descriptions: a query word with
diacritics matches only the same word as written. In running text the unaccented form is
usually another word ("nhậu" / "nhau", "chợ" / "cho"), while a name written without accents
("Pho Quynh") still means the dish, so names keep the tolerant rule.
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


# Tone marks (not the vowel marks of â, ă, ê, ô, ơ, ư): grave, acute, tilde, hook, dot below.
_TONES = frozenset("\u0300\u0301\u0303\u0309\u0323")
# Open syllables ending in these vowel pairs take the tone on either vowel: the old and new
# spellings "hòa"/"hoà", "khỏe"/"khoẻ", "thủy"/"thuỷ" are both in use (172 and 92 descriptions).
_FREE_PAIRS = frozenset({"oa", "oe", "uy"})


def tone_variant(word: str) -> str | None:
    """The same word with its tone mark on the other vowel of a final oa/oe/uy ("hoà" <->
    "hòa"); None when the placement is fixed ("hoàng", "ngoài", "quý": qu is the consonant)."""
    letters: list[list[str]] = []  # [base letter, its combining marks]
    for char in unicodedata.normalize("NFD", word):
        if unicodedata.combining(char) and letters:
            letters[-1][1] += char
        else:
            letters.append([char, ""])
    if len(letters) < 2:
        return None
    (first, first_marks), (second, second_marks) = letters[-2], letters[-1]
    after_q = first == "u" and len(letters) > 2 and letters[-3][0] == "q"
    if first + second not in _FREE_PAIRS or after_q:
        return None
    marks = first_marks + second_marks
    if len(marks) != 1 or marks not in _TONES:  # no tone, or a vowel mark that fixes it
        return None
    letters[-2][1], letters[-1][1] = ("", marks) if first_marks else (marks, "")
    return unicodedata.normalize("NFC", "".join(base + mark for base, mark in letters))


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


def lexeme_query(text: str | None, strict: bool = False) -> str | None:
    """tsquery literal matching the words of ``text`` as a phrase under the accent rule.
    ``strict``: an accented word matches only itself as written (see the module docstring); an
    unaccented word matches as in the tolerant rule. Either way an accented word also matches
    its other tone placement (tone_variant: "hoà" finds "hòa"), on the query side only, so the
    stored lexemes need no rebuild.

    None when the text has no words.
    """
    words = fold_case(text).split()
    if not words:
        return None
    parts = []
    for word in words:
        if not has_diacritics(word):
            parts.append(f"{_quote(word)}:AB")
            continue
        # Either tone placement of the word as written, then (tolerant) its folded form.
        forms = [word, *filter(None, [tone_variant(word)]), *([] if strict else [norm(word)])]
        quoted = [f"{_quote(form)}:A" for form in forms]
        parts.append(quoted[0] if len(quoted) == 1 else f"({' | '.join(quoted)})")
    return " <-> ".join(parts)
