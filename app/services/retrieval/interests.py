"""Turn an extraction's interests, ``other`` atoms and avoid list into retrieval requests.

Pure: the only outside input is the set of cuisine values present in the data, so an interest
such as "ramen" can filter ``cuisine`` even when the keyword map does not list it.

A wish that matches nothing as a whole ("kiếm chỗ uống rượu") is searched for the longest group
alias or standard-interest alias inside it, as whole words under a strict accent rule (compare
text.py): a word typed with accents must equal the alias word as written, and an unaccented word
matches an accented alias word only when the whole wish is unaccented. So "bìa rừng" is not
"bia" and "cùng nhau" is not "nhậu", while "kiem cho uong ruou" still holds "uống rượu".
The text request that comes with such a group (InterestRequest.companion) only ranks places
inside the group's section; see sections.py.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Literal

from app.schemas.extraction import Extraction
from app.services.extraction.processing import NAME_ALIASES, place_key
from app.services.retrieval.config import INTEREST_ALIASES, INTEREST_MAP, KEYWORD_MAP, Keyword
from app.services.retrieval.text import fold_case, has_diacritics, norm

Strength = Literal["include", "prefer"]
Source = Literal["interest", "other"]

# A free-text `other eq` that states a fact (a foreign budget, next-day availability: "budget
# exactly 75 NZD", "available 20:40 to 01:10"), not a wish: a currency word next to a number, a
# number with a budget word, a sum ("200k", "$75") or a clock time. A currency word alone is a
# place word ("cố đô" = the old capital, "thủ đô", "đô thị"), and so is a number alone ("quận 1",
# "cà phê 24h"). Compared as written: "đô" (dollar) is not "đồ" (thing).
CURRENCY_WORDS = frozenset({
    "usd", "eur", "chf", "nzd", "aud", "sgd", "jpy", "krw", "thb", "gbp", "vnd", "vnđ",
    "dollar", "dollars", "euro", "euros", "đô", "đôla", "đồng", "k", "triệu", "nghìn", "ngàn",
})
BUDGET_PHRASES = ("budget", "ngân sách", "chi phí", "price", "cost", "costs", "spend")
# On the raw words (fold_case would drop "$" and ":"): "200k", "$75", "1.5tr"; "20:40", "9h30".
_SUM = re.compile(r"^(\$\d[\d.,]*|\d[\d.,]*(k|đ|usd|vnd|vnđ|\$|tr))$")
_CLOCK = re.compile(r"\b\d{1,2}[:h]\d{2}\b|\b\d{1,2} ?(am|pm)\b")
IGNORED_WISHES = frozenset(norm(value) for value in KEYWORD_MAP.ignored_wishes)
Phrase = tuple[str, ...]  # words as written (fold_case)


def _phrase_index() -> tuple[tuple[Phrase, str], ...]:
    """(alias words, group key) of every group alias and standard-interest alias, longest
    first (words, then characters)."""
    index: dict[Phrase, str] = {}
    keywords = {keyword.key: keyword for keyword in KEYWORD_MAP.by_alias.values()}
    pairs = [
        *((alias, kw.group) for kw in keywords.values() if kw.group for alias in kw.written),
        *INTEREST_ALIASES.items(),
    ]
    for alias, group in pairs:
        words = tuple(alias.split())
        if index.setdefault(words, group) != group:
            raise ValueError(f"Alias {alias!r} routes to two groups")
    return tuple(sorted(index.items(), key=lambda item: (-len(item[0]), -len(" ".join(item[0])))))


GROUP_PHRASES = _phrase_index()
NOT_ALIASES = tuple(tuple(phrase.split()) for phrase in KEYWORD_MAP.not_aliases)
IGNORED_PHRASES = tuple(
    sorted((tuple(value.split()) for value in KEYWORD_MAP.ignored_wishes), key=len, reverse=True)
)
FILLER_WORDS = frozenset(KEYWORD_MAP.filler_words)
FILLER_FOLDED = frozenset(map(norm, KEYWORD_MAP.filler_words))
ACCENT_ONLY = frozenset(KEYWORD_MAP.accent_only)


@dataclass(frozen=True)
class InterestRequest:
    """One thing the traveller asked for.

    Exactly one of: ``group`` (taxonomy-node section), ``keyword`` (structured column filter,
    optionally plus text search) or ``text`` (normalized full-text query).
    """

    raw: str
    strength: Strength
    source: Source
    group: str | None = None
    keyword: Keyword | None = None
    text: str | None = None
    # A text request made together with this group's request ("bia" -> nightlife, "kiếm chỗ
    # uống rượu" -> nightlife): the group answers the wish, the words rank inside it. Always an
    # `other` request, so finding nothing by words stays silent.
    companion: str | None = None

    @property
    def key(self) -> str:
        if self.group:
            return self.group
        if self.keyword:
            return f"kw:{self.keyword.key}"
        return f"kw:{(self.text or '').replace(' ', '_')}"


@dataclass(frozen=True)
class AvoidRules:
    nodes: frozenset[str] = frozenset()  # taxonomy nodes, e.g. entertainment.nightlife for "bar"
    filters: tuple[Keyword, ...] = ()
    names: tuple[str, ...] = ()  # left for name resolution


@dataclass
class InterestPlan:
    requests: list[InterestRequest] = field(default_factory=list)
    avoid: AvoidRules = field(default_factory=AvoidRules)


def _interest_group(value: str) -> str | None:
    """Canonical standard interest for a value, via the extraction alias table."""
    standard = NAME_ALIASES["interest"].get(place_key(value), value)
    return standard if standard in INTEREST_MAP.groups else None


def _keyword(value: str, cuisines: dict[str, str]) -> Keyword | None:
    key = norm(value)
    if key in KEYWORD_MAP.by_alias:
        return KEYWORD_MAP.by_alias[key]
    if key in cuisines:
        raw = cuisines[key]
        label = KEYWORD_MAP.cuisine_labels.get(raw, value)
        return Keyword(f"cuisine_{raw}", label, (key,), {"cuisine": (raw,)})
    return None


def _kind_words(value: str, keyword: Keyword) -> str:
    """The words a kind's companion searches: the traveller's own, or the alias as written when
    they typed it without accents ("nhau" -> "nhậu": the unaccented word would match "cùng
    nhau" in every description), without the filler words they start with ("đi nhậu" ->
    "nhậu": no place is described as "đi nhậu")."""
    if not has_diacritics(fold_case(value)):
        value = next((alias for alias in keyword.written if norm(alias) == norm(value)), value)
    words = value.split()
    while len(words) > 1 and fold_case(words[0]) in FILLER_WORDS:
        words = words[1:]
    return " ".join(words)


def exact_requests(
    value: str, strength: Strength, source: Source, cuisines: dict[str, str]
) -> list[InterestRequest]:
    """The whole value read as a standard interest, a keyword or else free text. A group
    keyword with ``text`` ("bia") adds a text request for the traveller's words (_kind_words:
    "đi nhậu" and "di nhau" search "nhậu"); it is an `other` request, so finding nothing by
    words stays silent (the group answers the wish)."""
    if group := _interest_group(value):
        return [InterestRequest(value, strength, source, group=group)]
    if keyword := _keyword(value, cuisines):
        if not keyword.group:
            return [InterestRequest(value, strength, source, keyword=keyword)]
        requests = [InterestRequest(value, strength, source, group=keyword.group)]
        words = _kind_words(value, keyword)
        if keyword.text and (text := norm(words)):
            requests.append(
                InterestRequest(words, strength, "other", text=text, companion=keyword.group)
            )
        return requests
    text = norm(value)
    return [InterestRequest(value, strength, source, text=text)] if text else []


def is_fact(value: str) -> bool:
    """An `other eq` that states a fact rather than a wish (see CURRENCY_WORDS)."""
    raw = value.casefold()
    if _CLOCK.search(raw) or any(_SUM.match(word.strip(".,;:!?()")) for word in raw.split()):
        return True
    words = fold_case(value).split()
    numbers = {n for n, word in enumerate(words) if any(char.isdigit() for char in word)}
    if not numbers:
        return False
    if any(word in CURRENCY_WORDS and numbers & {n - 1, n + 1} for n, word in enumerate(words)):
        return True
    return any(f" {phrase} " in f" {' '.join(words)} " for phrase in BUDGET_PHRASES)


def _find(words: list[str], phrase: Phrase, loose: bool) -> int | None:
    """Start of ``phrase`` in ``words`` as whole words under the accent rule; ``loose``: the
    wish has no accents, so an accented alias word matches its folded form (not an accent_only
    word: "den" is đến far more often than đền)."""
    size = len(phrase)
    for start in range(len(words) - size + 1):
        if all(
            word == alias or (loose and alias not in ACCENT_ONLY and word == norm(alias))
            for word, alias in zip(words[start : start + size], phrase, strict=True)
        ):
            return start
    return None


def _cut(words: list[str], phrases: tuple[Phrase, ...], loose: bool) -> tuple[list[str], bool]:
    """``words`` without every occurrence of ``phrases`` (longest first); whether any was cut."""
    cut = False
    for phrase in phrases:
        while (start := _find(words, phrase, loose)) is not None:
            words = [*words[:start], *words[start + len(phrase) :]]
            cut = True
    return words, cut


def _contained(words: list[str]) -> tuple[str, int, int] | None:
    """(group, start, size) of the alias contained_group picks in ``words``."""
    loose = not any(has_diacritics(word) for word in words)
    words = list(words)
    # A not-alias is masked, not cut, so its words cannot join a phrase across the gap.
    for phrase in NOT_ALIASES:
        while (start := _find(words, phrase, loose)) is not None:
            words[start : start + len(phrase)] = [""] * len(phrase)
    found = [
        (group, start, len(phrase))
        for phrase, group in GROUP_PHRASES
        if (start := _find(words, phrase, loose)) is not None
    ]
    with_data = (item for item in found if INTEREST_MAP.groups[item[0]].nodes)
    return next(with_data, None) or next(iter(found), None)


def contained_group(value: str) -> str | None:
    """The group of the longest group alias or standard-interest alias inside ``value`` (see
    the module docstring), ignoring the not_aliases phrases ("bia tiến sĩ"). A group with data
    beats a longer alias of one without: "beach bar" is a bar, there are no beaches to show."""
    # semantic imports sections, which imports this module.
    from app.services.retrieval.semantic import words_of

    found = _contained(words_of(value))
    return found[0] if found else None


def _only_filler(words: list[str]) -> bool:
    """Whether ``words`` add nothing to an alias (keyword_map filler_words, accent rule)."""
    loose = not any(has_diacritics(word) for word in words)
    return all(
        word in FILLER_WORDS or (loose and word in FILLER_FOLDED) for word in words if word
    )


def wish_requests(
    value: str, strength: Strength, source: Source, cuisines: dict[str, str], *, eq: bool = False
) -> list[InterestRequest]:
    """Requests for one interest or `other` atom (``eq``: the atom's operator is eq).

    A time ("tối nay") makes none, nor does a free-text eq fact. Free text is also routed to
    the group of an alias inside it: "kiếm chỗ uống rượu" -> nightlife plus the text request
    as its companion, so semantic search ranks inside the group. A group without data (beach)
    keeps only its request and its no-data warning when the rest of the wish is filler ("bãi
    biển", "đi tắm biển"): places named "Biển" are not beaches. With more words ("ăn hải sản
    ven biển") the text is still searched. A negation before the alias is a refusal ("không
    uống rượu", "tránh xa bar") and is never routed; one after it qualifies the place ("quán
    bar không ồn ào") and is.
    """
    if norm(value) in IGNORED_WISHES:
        return []
    requests = exact_requests(value, strength, source, cuisines)
    if len(requests) != 1 or requests[0].text is None:
        return requests
    if eq and is_fact(value):
        return []
    words = fold_case(value).split()
    words, cut = _cut(words, IGNORED_PHRASES, not any(has_diacritics(word) for word in words))
    if cut:
        # "tối nay đi nhậu" is read as "đi nhậu", and its section is not named after the time.
        return wish_requests(" ".join(words), strength, source, cuisines) if words else []
    # semantic imports sections, which imports this module.
    from app.services.retrieval.semantic import first_negation, words_of

    words = words_of(value)
    found = _contained(words)
    negation = first_negation(value)
    if found is None or (negation is not None and negation < found[1]):
        return requests
    group, start, size = found
    routed = InterestRequest(value, strength, source, group=group)
    # The companion is an `other` request whatever the atom: the group answers the wish, so a
    # text search that finds nothing (or no embedder) is not a "not found".
    companion = replace(requests[0], source="other", companion=group)
    rest = [*words[:start], *words[start + size :]]
    if not INTEREST_MAP.groups[group].nodes and _only_filler(rest):
        return [routed]
    return [routed, companion]


def _strength(extraction: Extraction, field_name: str, value: str) -> Strength:
    hard = any(
        atom.field == field_name and atom.value == value and atom.operator in ("include", "eq")
        for atom in extraction.hard_constraints
    )
    return "include" if hard else "prefer"


def _avoid_rules(values: list[str], cuisines: dict[str, str]) -> AvoidRules:
    nodes: set[str] = set()
    filters: list[Keyword] = []
    names: list[str] = []
    for value in values:
        if group_key := _interest_group(value):
            nodes.update(INTEREST_MAP.groups[group_key].nodes)
        elif keyword := _keyword(value, cuisines):
            if keyword.group:
                nodes.update(INTEREST_MAP.groups[keyword.group].nodes)
            else:
                filters.append(keyword)
        else:
            names.append(value)
    return AvoidRules(frozenset(nodes), tuple(filters), tuple(names))


def plan_interests(extraction: Extraction, cuisines: dict[str, str] | None = None) -> InterestPlan:
    """Requests in the traveller's order, deduplicated by section key (include wins)."""
    cuisines = cuisines or {}
    candidates: list[InterestRequest] = []
    for value in extraction.interests:
        strength = _strength(extraction, "interests", value)
        candidates.extend(wish_requests(value, strength, "interest", cuisines))
    atom_lists: tuple[tuple[Strength, list], ...] = (
        ("include", extraction.hard_constraints),
        ("prefer", extraction.soft_constraints),
    )
    for atom_strength, atoms in atom_lists:
        for atom in atoms:
            if atom.field != "other" or atom.operator in ("exclude", "min", "max"):
                continue
            # /chat files plain wishes as eq too ("kiếm chỗ uống rượu"); only a fact is skipped.
            eq = atom.operator == "eq"
            candidates.extend(wish_requests(atom.value, atom_strength, "other", cuisines, eq=eq))

    by_key: dict[str, InterestRequest] = {}
    for request in candidates:
        current = by_key.get(request.key)
        if current is None or (current.strength == "prefer" and request.strength == "include"):
            by_key[request.key] = request if current is None else _keep_first_raw(current, request)
    # `other` atoms that excluded something structured become avoid filters as well.
    other_avoid = [
        atom.value
        for atom in extraction.hard_constraints
        if atom.field == "other" and atom.operator == "exclude" and _keyword(atom.value, cuisines)
    ]
    avoid = _avoid_rules([*extraction.avoid, *other_avoid], cuisines)
    return InterestPlan(list(by_key.values()), avoid)


def _keep_first_raw(current: InterestRequest, stronger: InterestRequest) -> InterestRequest:
    return InterestRequest(
        current.raw,
        "include",
        current.source,
        stronger.group,
        stronger.keyword,
        stronger.text,
        current.companion or stronger.companion,
    )
