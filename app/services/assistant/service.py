"""Journie assistant: find places and guides for the traveller's question, then let Gemini
answer in Journie's voice, grounded on them.

Retrieval never fails the answer: without embeddings the places come from full-text search,
without the database there are none (meta.semantic is false either way). No connection is held
while waiting for Gemini.
"""

import csv
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from app.core.errors import ProblemError
from app.schemas.assistant import (
    RESPONSE_SCHEMA,
    AssistantGuide,
    AssistantMessage,
    AssistantMeta,
    AssistantPlace,
    AssistantRequest,
    AssistantResponse,
    ModelAnswer,
)
from app.services.assistant import prompt
from app.services.assistant.knowledge import KNOWLEDGE, Knowledge
from app.services.extraction.service import TIMEZONE, InvalidModelOutputError
from app.services.gemini import GeminiClient, GeminiError
from app.services.retrieval import semantic
from app.services.retrieval.candidates import ELIGIBLE, SUGGESTABLE, eligibility
from app.services.retrieval.config import NAME_ALIASES_CSV
from app.services.retrieval.db import Connect
from app.services.retrieval.interests import AvoidRules
from app.services.retrieval.scoring import MICHELIN_LABELS, quality_score
from app.services.retrieval.service import DEFAULT_DESTINATION, SUPPORTED_DESTINATIONS
from app.services.retrieval.text import fold_case, lexeme_query, norm

logger = logging.getLogger(__name__)

PLACES_K = 8
# Whole questions sit lower than the short interest phrases semantic search was tuned on
# (SIMILARITY_MIN 0.651). Measured on 2026-11-24: "Tối nay đi đâu uống cocktail ở Sài Gòn?" tops
# at 0.776 with 10 bars above 0.73; "Where can I eat good pho for breakfast?" (Hanoi) 0.685-0.711,
# all phở; "Trời mưa thì đi đâu ở Huế?" 0.710-0.738; "Đà Nẵng có bãi biển nào đẹp không?" (no
# beach in the data) 0.685-0.715, seafood places and sky bars; the off-topic "Giải giúp mình bài
# toán lớp 9" tops at 0.592. 0.62 keeps every travel question and drops the off-topic one.
PLACE_SIMILARITY_MIN = 0.62
# The model sees PLACES_K places, picked from this many of the closest ones. What the question asks
# comes first: only places about as close as the closest one count (within FIT_WINDOW of cosine
# similarity; 2026-11-24 questions spread their good places over 0.03-0.05), ranked 70% by
# closeness and 30% by quality_score (tier, source count, Michelin, the trip's sections use it
# too). Places no source recommends are left out when RECOMMENDED_ENOUGH recommended ones fit;
# a kind with fewer (bars or parks in some cities) keeps them, so the answer is not empty.
CANDIDATES_K = PLACES_K * 3
FIT_WINDOW = 0.06
QUALITY_SHARE = 0.3
RECOMMENDED_ENOUGH = 3
# A follow-up this short ("còn quán nào khác?") is searched together with the previous question.
SHORT_QUESTION_WORDS = 6
# Questions searched together at most: the latest and two it follows up on, so "Còn quán nào
# khác?" then "Trong mấy quán đó..." still carries the first question's "phở ở Hà Nội".
SEARCHED_QUESTIONS = 3
DESCRIPTION_CHARS = 280
COVERAGE_TTL_SECONDS = 300
# Full-text fallback: words that would match most places or name the city (already filtered).
_STOPWORD_TEXT = """
    à ạ ai anh bạn bây bên bị các cái cần cho chỗ chứ có của cùng cũng đã đang đâu để đến đi
    được gì giờ giúp gợi hay hãy hôm không là làm lại mà mấy mình một muốn nay nào nên nha nhé
    như những nơi ơi ở quán ra rồi sao thì thế tìm tôi trong từ và vào về với vừa xem ý
    sài gòn saigon hcm tphcm tp chí minh hà nội hanoi đà nẵng danang huế hue thành
    a an and any are at be can do for from go good i in is it me my of on or place places some
    the to want what where which with you your
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
MAX_KEYWORD_TERMS = 16
# Database down, pool exhausted (get_connection turns both into a 503 ProblemError), SQL error.
RETRIEVAL_ERRORS = (psycopg.Error, PoolTimeout, ProblemError, OSError)


@dataclass(frozen=True, slots=True)
class Place:
    id: str
    name: str
    category: str
    name_en: str | None = None
    ward: str | None = None
    description: str | None = None
    opening_hours_raw: str | None = None
    price_raw: str | None = None
    cuisine: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    # How well recommended the place is; read by retrieval's quality_score (same field names as Poi)
    tier: str | None = None
    recommended_by_count: int | None = None
    michelin: str | None = None
    confidence: float | None = None

    def display_name(self, language: str) -> str:
        return self.name_en if language == "en" and self.name_en else self.name

    def context(self) -> dict[str, Any]:
        """The place as the model sees it; empty fields are left out."""
        fields: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "name_en": self.name_en,
            "category": self.category,
            "area": self.ward,
            "about": shorten(self.description, DESCRIPTION_CHARS),
            "hours": self.opening_hours_raw,
            "price": self.price_raw,
            "cuisine": list(self.cuisine),
            "features": list(self.features),
            "recommended_by_sources": self.recommended_by_count
            if (self.recommended_by_count or 0) >= 2
            else None,
            "michelin": MICHELIN_LABELS.get(self.michelin or ""),
        }
        return {key: value for key, value in fields.items() if value}

    def card(self, language: str) -> AssistantPlace:
        return AssistantPlace(
            id=self.id, name=self.display_name(language), category=self.category, area=self.ward
        )


@dataclass(frozen=True, slots=True)
class Found:
    places: list[Place]
    semantic: bool
    vector: list[float] | None
    coverage: dict[str, int] | None  # None: the database could not be read


def shorten(text: str | None, limit: int) -> str | None:
    if not text or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def folded(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


# A follow-up that points back at what was just named ("Trong mấy quán đó, quán nào mở sớm
# nhất?", "Which of those opens early?") asks about those places however long it is, so it is
# searched with the question that found them. "trên" counts only at the end of the phrase:
# "quán trên phố cổ" is a place on a street. Unaccented "cho" (for) only with an unaccented
# pointer, as in "cho do" (chỗ đó), not "cho trên 10 người". "Bạn gợi ý ..." opens most new
# questions, so "bạn gợi ý" needs "vừa/mới/đã", "lúc nãy" or a place before it ("chỗ bạn gợi
# ý"). "trong đó" mid-sentence is "of which" ("3 ngày, trong đó có một ngày mưa"), so only at
# the start. "them" is often people ("take them for dinner"), so only as "of them".
_REFERS_BACK_TEXT = r"""
    (?:quán|chỗ|nơi|điểm|nhà hàng|tiệm|hàng|món|cái|bar) (?:đó|đấy|ấy|kia|này|vừa rồi|vừa nãy)
    (?:quán|chỗ|nơi|nhà hàng|tiệm) trên(?=\s*(?:[,.?!;:]|$|thì |có |nào ))
    (?:quan|cho|noi|nha hang|tiem|mon|cai) (?:do|day|kia|nay)
    trong (?:số|so) (?:đó|đấy|do|day)
    ^trong (?:đó|đấy|do|day)
    (?:ở|tới|đến|qua|ra) (?:đó|đấy)
    bạn (?:vừa|mới|đã|vừa mới) (?:gợi ý|kể|nói|nhắc|liệt kê)
    (?:gợi ý|kể|nói|nhắc|liệt kê) (?:lúc nãy|hồi nãy|vừa rồi|ở trên|bên trên)
    (?:quán|chỗ|nơi|tiệm|nhà hàng) (?:mà )?bạn (?:gợi ý|kể|nói|nhắc|liệt kê)
    ban (?:vua|moi) (?:goi y|ke|noi|nhac|liet ke)
    nó
    those|these|(?:of|among) them
    (?:that|this|the first|the second|the third|the last) (?:one|place|spot|restaurant|cafe|bar)
    (?:you|u) (?:just )?(?:mentioned|suggested|recommended|listed|named)
"""
_REFERS_BACK = re.compile(
    r"(?<!\w)(?:"
    + "|".join(line.strip().replace(" ", r"\s+") for line in _REFERS_BACK_TEXT.strip().split("\n"))
    + r")(?!\w)"
)
# Picking one out with no new kind of place and no city of its own ("Quán nào mở cửa sớm nhất
# vậy bạn?", "Which one opens earliest?") is about the places just named. "Có quán nào yên
# tĩnh không?" (no "nhất") and "Quán cà phê nào đẹp nhất?" (a new kind) are new questions, so
# is "Chỗ nào đẹp nhất để ngắm hoàng hôn?": "chỗ/nơi nào" is too broad to count.
_PICKS_AMONG = re.compile(
    r"(?<!\w)(?:(?:quán|tiệm|nhà hàng) nào|(?:quan|tiem|nha hang) nao)(?!\w)"
    r"[^.?!]*?(?<!\w)(?:nhất|nhat)(?!\w)"
    r"|(?<!\w)which\s+(?:one|ones)(?!\w)(?!\s+of\s)"
)


def follows_up(question: str) -> bool:
    """Whether a question leans on the one before it: a short one ("còn quán nào khác?") or one
    that points back at the places just named."""
    if len(question.split()) < SHORT_QUESTION_WORDS:
        return True
    text = folded(question).strip()
    if _REFERS_BACK.search(text):
        return True
    return bool(_PICKS_AMONG.search(text)) and not cities_named(text)


def _asked(messages: list[AssistantMessage]) -> list[str]:
    return [message.text.strip() for message in messages if message.role == "user"]


def _thread_start(asked: list[str]) -> int:
    """Index of the question the latest one's chain of follow-ups started from."""
    start = len(asked) - 1
    while start > 0 and follows_up(asked[start]):
        start -= 1
    return start


def questions(messages: list[AssistantMessage]) -> list[str]:
    """The latest question, with the ones it follows up on in front (SEARCHED_QUESTIONS at most)."""
    asked = _asked(messages)
    return asked[max(_thread_start(asked), len(asked) - SEARCHED_QUESTIONS) :]


def answered_before(messages: list[AssistantMessage]) -> tuple[str, str] | None:
    """For a follow-up, the search text the reply before it was answered from, and that reply:
    the places it named are looked up again, as the follow-up's own search may miss them."""
    if len(messages) < 3 or messages[-2].role != "assistant" or not follows_up(messages[-1].text):
        return None
    earlier = messages[:-2]
    last_asked = max((i for i, message in enumerate(earlier) if message.role == "user"), default=-1)
    if last_asked < 0:
        return None
    return retrieval_text(earlier[: last_asked + 1]), messages[-2].text


def retrieval_text(messages: list[AssistantMessage]) -> str:
    return " ".join(questions(messages))


_SEPARATORS = r"[\s.\-_]+"
# A bare city name ("Huế", "Hà Nội", "Hanoi") names the place only at the start of a sentence or
# after a word of place or movement ("ở Huế", "đi Hà Nội", "in Hanoi"). Elsewhere it is often part
# of another name: "bún bò Huế", "phở Hà Nội", "phố Huế" (a street in Hanoi), "Nguyen Hue Street",
# "Lăng Chủ tịch Hồ Chí Minh". Qualified names ("TP Huế", "Hue City", "TP.HCM") always count.
# Also after a verb of liking or going ("thích Đà Nẵng", "tham quan Huế", "bay Đà Nẵng"), the
# same words typed without accents ("o ha noi"), and English search words ("coffee shops
# Hanoi"). Not "từ": "bay từ Hà Nội vào Đà Nẵng" asks about Da Nang. Not a bare unaccented
# "quan": "quan hue" is quán Huế (a Huế-style eatery), so only "tham quan".
_PLACE_WORD_TEXT = """
    ở tại đi đến tới ra vào về qua quanh khắp trong ngoài chơi thăm ghé lịch trình phá còn và
    thích yêu mê bay lên xuống
    o tai di den toi vao ve khap ngoai choi tham ghe lich trinh pha con va thich yeu len xuong
    in at to visit visiting around near about for is and love explore exploring
    shops shop cafes cafe coffee bars restaurants restaurant food eats spots places hotels
    attractions sights nightlife museums markets beaches temples
"""
PLACE_WORDS = frozenset(_PLACE_WORD_TEXT.split())
# A liked city or a cuisine ("I love Hue food, where can I get it in Saigon?") may not be where
# the traveller asks about: it counts only when no other city is named plainly.
LIKING_WORDS = frozenset({"thích", "yêu", "mê", "thich", "yeu", "love"})
_CUISINE_AFTER = re.compile(r"[\s-]*(?:food|cuisine|style|dishes|dish|specialties|cooking)(?!\w)")
_THAM_QUAN = re.compile(r"(?<!\w)tham\s+quan\s*$")
_QUALIFIERS = frozenset({"thành", "thanh", "tp", "city"})
_SENTENCE_START = re.compile(r"(?:^|[.,!?;:()\n\"“”'-])\s*$")
_LAST_WORD = re.compile(r"(\w+)\s*$")
# English puts the landmark after the name: "Ho Chi Minh Mausoleum" is in Hanoi.
_LANDMARK_AFTER = re.compile(r"\s*(?:mausoleum|trail|statue)(?!\w)")


def _alias_pattern(alias: str) -> str:
    """The alias as a pattern; "TP. HCM" also matches "tphcm" and "tp-hcm". Accents are kept,
    so "huệ" (as in Nguyễn Huệ) is not Huế."""
    parts = re.split(_SEPARATORS, unicodedata.normalize("NFC", alias).casefold())
    return r"[\s.\-_]*".join(re.escape(part) for part in parts if part)


def _is_qualified(alias: str) -> bool:
    parts = re.split(_SEPARATORS, unicodedata.normalize("NFC", alias).casefold())
    return any(part in _QUALIFIERS or "hcm" in part for part in parts)


def _compile(aliases: list[str]) -> re.Pattern[str] | None:
    if not aliases:
        return None
    alternatives = sorted(map(_alias_pattern, aliases), key=len, reverse=True)
    return re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)")


@dataclass(frozen=True, slots=True)
class CityNames:
    qualified: re.Pattern[str] | None
    bare: re.Pattern[str] | None

    def first(self, text: str) -> tuple[int, int] | None:
        """Where ``text`` (NFC, casefolded) first names the city, as (rank, position): rank 1
        when it is named only after a verb of liking or before a cuisine word; None if not."""
        found = []
        if self.qualified and (match := self.qualified.search(text)):
            found.append((0, match.start()))
        for match in self.bare.finditer(text) if self.bare else ():
            if _LANDMARK_AFTER.match(text, match.end()):
                continue
            before = text[: match.start()]
            word = _LAST_WORD.search(before)
            last = word.group(1) if word else None
            if not (
                _SENTENCE_START.search(before) or last in PLACE_WORDS or _THAM_QUAN.search(before)
            ):
                continue
            weak = last in LIKING_WORDS or bool(_CUISINE_AFTER.match(text, match.end()))
            found.append((int(weak), match.start()))
            if not weak:
                break
        return min(found, default=None)


def load_city_names() -> dict[str, CityNames]:
    """Each supported destination -> its names in the extraction's alias table."""
    names = {destination: {destination} for destination in SUPPORTED_DESTINATIONS}
    with NAME_ALIASES_CSV.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["kind"] == "destination" and row["canonical"] in names:
                names[row["canonical"]].add(row["alias"])
    return {
        destination: CityNames(
            qualified=_compile([alias for alias in aliases if _is_qualified(alias)]),
            bare=_compile([alias for alias in aliases if not _is_qualified(alias)]),
        )
        for destination, aliases in names.items()
    }


CITY_NAMES = load_city_names()


def cities_named(text: str) -> list[str]:
    """Supported destinations the text names as places, in the order it first names them; one
    named only after a verb of liking or before a cuisine word when no other is named plainly."""
    text = folded(text)
    found = sorted(
        (*first, destination)
        for destination, names in CITY_NAMES.items()
        if (first := names.first(text)) is not None
    )
    return [destination for rank, _, destination in found if rank == found[0][0]]


def asked_destination(messages: list[AssistantMessage], destination: str) -> str:
    """The supported city the question is about. The app sends the city picked on screen, but
    "Ở Hà Nội ăn phở ở đâu?" asks about Hanoi wherever the app is: a city the latest question
    names wins (the app's one when it is among several), then the city named last by the
    questions it follows up on, so "Trong mấy quán đó, quán nào mở sớm nhất?" stays in Hanoi,
    then the app's city. A new question that names none is about the city on screen, which the
    traveller may have switched since."""
    asked = _asked(messages)
    for text in reversed(asked[_thread_start(asked) :]):
        if named := cities_named(text):
            return destination if destination in named else named[0]
    return destination


def keyword_query(text: str) -> str | None:
    """tsquery for the full-text fallback: any content word, or two adjacent ones as a phrase
    ("cà phê", "hải sản"), under the strict accent rule of text.lexeme_query."""
    words = fold_case(text).split()
    content = [word if word not in STOPWORDS and not word.isdigit() else None for word in words]
    terms = [f"{a} {b}" for a, b in pairwise(content) if a and b]
    terms += [word for word in content if word and len(word) > 1]
    queries = [lexeme_query(term, strict=True) for term in dict.fromkeys(terms)]
    parts = [f"({query})" for query in queries if query][:MAX_KEYWORD_TERMS]
    return " | ".join(parts) or None


DETAIL_SQL = """
    SELECT id, name, name_en, COALESCE(category, kind) AS category, ward, description,
           opening_hours_raw, price_raw, cuisine, features, tier, recommended_by_count, michelin,
           confidence
    FROM pois
    WHERE id = ANY(%(ids)s) AND city = %(city)s
"""

KEYWORD_SQL = f"""
    SELECT id, name_lex @@ %(tsq)s::tsquery AS by_name, ts_rank(search_lex, %(tsq)s::tsquery) AS rank
    FROM pois
    WHERE {ELIGIBLE} AND {SUGGESTABLE}
      AND (search_lex @@ %(tsq)s::tsquery OR name_lex @@ %(tsq)s::tsquery)
    ORDER BY (name_lex @@ %(tsq)s::tsquery) DESC, ts_rank(search_lex, %(tsq)s::tsquery) DESC,
             recommended_by_count DESC NULLS LAST, id
    LIMIT %(k)s
"""

COVERAGE_SQL = f"""
    SELECT COALESCE(category, kind) AS category, count(*) FROM pois
    WHERE {ELIGIBLE} AND {SUGGESTABLE}
    GROUP BY 1
    ORDER BY 2 DESC, 1
"""

# shortcut: per-process cache like candidates.data_info; a new import shows after the TTL.
_coverage: dict[str, tuple[float, dict[str, int]]] = {}


def _params(city: str) -> dict[str, Any]:
    return eligibility(AvoidRules(), set(), city)


async def city_coverage(conn: AsyncConnection, city: str) -> dict[str, int]:
    """Suggestable places of the city per category."""
    now = time.monotonic()
    cached = _coverage.get(city)
    if cached is None or now - cached[0] > COVERAGE_TTL_SECONDS:
        async with conn.cursor() as cur:
            await cur.execute(COVERAGE_SQL, _params(city))
            cached = (now, {category: count for category, count in await cur.fetchall()})
        _coverage[city] = cached
    return cached[1]


async def place_details(conn: AsyncConnection, ids: list[str], city: str) -> list[Place]:
    if not ids:
        return []
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(DETAIL_SQL, {"ids": ids, "city": city})
        rows = {row["id"]: row for row in await cur.fetchall()}
    return [
        Place(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            name_en=row["name_en"],
            ward=row["ward"],
            description=row["description"],
            opening_hours_raw=row["opening_hours_raw"],
            price_raw=row["price_raw"],
            cuisine=tuple(row["cuisine"] or ()),
            features=tuple(row["features"] or ()),
            tier=row["tier"],
            recommended_by_count=row["recommended_by_count"],
            michelin=row["michelin"],
            confidence=row["confidence"],
        )
        for poi_id in ids
        if (row := rows.get(poi_id))
    ]


def recommended(place: Place) -> bool:
    return (place.recommended_by_count or 0) > 0 or bool(place.michelin)


def best_places(places: list[Place], fit: dict[str, float]) -> list[Place]:
    """The PLACES_K best of ``places``: how well each fits the question (``fit``, 1 for the
    closest) first, then how well it is recommended; equal scores keep the search's order."""
    good = [place for place in places if recommended(place)]
    pool = good if len(good) >= RECOMMENDED_ENOUGH else places
    at = {place.id: i for i, place in enumerate(pool)}

    def score(place: Place) -> float:
        return (1 - QUALITY_SHARE) * fit.get(place.id, 0.0) + QUALITY_SHARE * quality_score(
            place  # type: ignore[arg-type]  # Place has the Poi fields quality_score reads
        )

    return sorted(pool, key=lambda place: (-score(place), at[place.id]))[:PLACES_K]


async def semantic_places(conn: AsyncConnection, vector: list[float], city: str) -> list[Place]:
    """The PLACES_K eligible, suggestable places of the city that fit the question best: of the
    CANDIDATES_K closest, those about as close as the closest one, closest and best recommended
    first."""
    async with conn.cursor() as cur:
        await cur.execute(
            semantic.SEMANTIC_SQL,
            {
                **_params(city),
                "vector": semantic.vector_literal(vector),
                "k": CANDIDATES_K,
                "min": PLACE_SIMILARITY_MIN,
            },
        )
        found = sorted(await cur.fetchall(), key=lambda row: row[1], reverse=True)
    if not found:
        return []
    best = float(found[0][1])
    found = [(poi_id, sim) for poi_id, sim in found if float(sim) >= best - FIT_WINDOW]
    span = best - PLACE_SIMILARITY_MIN
    fit = {
        poi_id: (float(sim) - PLACE_SIMILARITY_MIN) / span if span > 0 else 1.0
        for poi_id, sim in found
    }
    return best_places(await place_details(conn, [poi_id for poi_id, _ in found], city), fit)


async def keyword_places(conn: AsyncConnection, text: str, city: str) -> list[Place]:
    """Full-text fallback: places whose name or search document shares the question's words."""
    tsquery = keyword_query(text)
    if tsquery is None:
        return []
    async with conn.cursor() as cur:
        await cur.execute(KEYWORD_SQL, {**_params(city), "tsq": tsquery, "k": CANDIDATES_K})
        rows = await cur.fetchall()
    places = await place_details(conn, [row[0] for row in rows], city)
    # A place the question names comes first; the others by how well they match and are recommended
    named = {row[0] for row in rows if row[1]}
    top = max((float(row[2]) for row in rows), default=0.0)
    fit = {row[0]: float(row[2]) / top if top > 0 else 1.0 for row in rows}
    first = [place for place in places if place.id in named]
    return (first + best_places([place for place in places if place.id not in named], fit))[
        :PLACES_K
    ]


async def find_places(
    connect: Connect | None, embed: semantic.Embed | None, question: str, city: str
) -> Found:
    vector: list[float] | None = None
    if embed is not None:
        query = semantic.QUERY_FORMAT.format(question)
        try:
            vector = (await semantic.QUERY_CACHE.get_many([query], embed))[query]
        except (GeminiError, ValueError) as exc:  # retrieval must never fail the answer
            logger.warning("Assistant question not embedded: %s", type(exc).__name__)
    if connect is None:
        return Found([], False, vector, None)
    try:
        async with connect() as conn:
            coverage = await city_coverage(conn, city)
            places = await semantic_places(conn, vector, city) if vector is not None else []
            from_semantic = bool(places)
            if not places:
                places = await keyword_places(conn, question, city)
    except RETRIEVAL_ERRORS as exc:  # answer without places
        logger.warning("Assistant retrieval failed: %s", type(exc).__name__)
        return Found([], False, vector, None)
    return Found(places, from_semantic, vector, coverage)


async def with_places_named_before(
    found: Found,
    connect: Connect | None,
    embed: semantic.Embed | None,
    city: str,
    before: tuple[str, str],
) -> tuple[Found, set[str]]:
    """``found`` plus the places the reply before named that it misses, from the same search
    that reply was answered from (its question's vector is cached), and the ids of the places
    that reply named: "Trong mấy quán đó, quán nào mở sớm nhất?" is about those places, which a
    search for the follow-up may not return."""
    question, reply = before
    earlier = await find_places(connect, embed, question, city)
    known = {place.id for place in found.places}
    added = [place for place in earlier.places if place.id not in known]
    named = named_in_full(found.places + added, norm(reply))
    return replace(found, places=found.places + [p for p in added if p.id in named]), named


def clean_reply(text: str) -> str:
    """Plain text as the contract has it: no headings or bold, "- " bullets."""
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^(\s*)[*•]\s+", r"\1- ", line)
        lines.append(line.rstrip())
    cleaned = "\n".join(lines).replace("**", "").replace("__", "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _named_in_full(name: str | None, reply: str) -> bool:
    key = norm(name)
    return bool(key) and f" {key} " in f" {reply} "


_ALSO_CALLED = re.compile(r"(.+?)\s*\([^()]*\)\s*")


def _spans(place: Place, reply: str) -> list[tuple[int, int]]:
    """Where the reply (norm()-ed) writes the place's name or English name in full; "Phở Bò Ấu
    Triệu (Phở Tư Lùn)" also as "Phở Bò Ấu Triệu", but a lone "Phở" is no name."""
    names = []
    for name in filter(None, (place.name, place.name_en)):
        names.append(norm(name))
        if (match := _ALSO_CALLED.fullmatch(name)) and len(short := norm(match[1]).split()) > 1:
            names.append(" ".join(short))
    padded, spans = f" {reply} ", []
    for key in filter(None, names):
        start = padded.find(f" {key} ")
        while start >= 0:
            spans.append((start, start + len(key) + 2))
            start = padded.find(f" {key} ", start + 1)
    return spans


def named_in_full(places: list[Place], reply: str) -> set[str]:
    """Ids of the places the reply (norm()-ed) names in full. Unlike mentioned(), shared words
    are not enough ("Phở Bò Lâm" is not "Phở Bò Ấu Triệu"), and a name inside a longer one the
    reply names is not that place ("Phở Gia Truyền" in "Phở Gia Truyền Bát Đàn")."""
    spans = {place.id: _spans(place, reply) for place in places}
    every = {span for found in spans.values() for span in found}

    def inside_longer(span: tuple[int, int]) -> bool:
        return any(a <= span[0] and span[1] <= b and b - a > span[1] - span[0] for a, b in every)

    return {pid for pid, found in spans.items() if any(not inside_longer(s) for s in found)}


def mentioned(place: Place, reply: str) -> bool:
    """Whether the reply (already norm()-ed) names the place: its whole name, or at least half
    of its words and at least two ("Lost Birds" for "Lost Birds Bar")."""
    words = set(reply.split())
    for name in filter(None, (place.name, place.name_en)):
        if _named_in_full(name, reply):
            return True
        parts = [word for word in norm(name).split() if len(word) > 1]
        hits = sum(word in words for word in parts)
        if len(parts) > 1 and hits >= max(2, math.ceil(len(parts) / 2)):
            return True
    return False


def pick_places(ids: list[str], found: list[Place], reply: str) -> list[Place]:
    """Retrieved places the reply names: the model's ids first (unknown ids and places the reply
    does not name are dropped), then retrieved places it names in full without listing them."""
    by_id = {place.id: place for place in found}
    text = norm(reply)
    picked = [by_id[i] for i in dict.fromkeys(ids) if i in by_id and mentioned(by_id[i], text)]
    picked += [
        place
        for place in found
        if place not in picked
        and any(
            len(norm(name).split()) > 1 and _named_in_full(name, text)
            for name in filter(None, (place.name, place.name_en))
        )
    ]
    return picked


def local_now() -> datetime:
    return datetime.now(TIMEZONE)


async def answer(
    client: GeminiClient,
    request: AssistantRequest,
    connect: Connect | None,
    embed: semantic.Embed | None,
    knowledge: Knowledge | None = None,
    *,
    timeout: float | None = None,
    fallback_model: str | None = None,
) -> AssistantResponse:
    """Answer the last message of ``request``.

    Raises:
        InvalidModelOutputError: The response was empty, blocked, truncated, or schema-invalid.
        GeminiError: The Gemini request itself failed.
    """
    knowledge = knowledge or KNOWLEDGE
    destination = asked_destination(request.messages, request.destination or DEFAULT_DESTINATION)
    city = SUPPORTED_DESTINATIONS[destination]
    question = retrieval_text(request.messages)
    found = await find_places(connect, embed, question, city)
    named_before: set[str] = set()
    if before := answered_before(request.messages):
        found, named_before = await with_places_named_before(found, connect, embed, city, before)
    guides = await knowledge.search(question, found.vector, embed)
    language = request.language
    user_turn = prompt.build_user_turn(
        city=f"{destination} ({city})",
        supported_cities=[f"{name} ({value})" for name, value in SUPPORTED_DESTINATIONS.items()],
        now=local_now(),
        language=language,
        coverage=found.coverage,
        # Marked, so "mấy quán đó" is answered about them and no other place passes for one
        places=[
            {**place.context(), "named_before": True}
            if place.id in named_before
            else place.context()
            for place in found.places
        ],
        guides=[{"id": c.id, "title": c.title(language), "text": c.text} for c in guides],
        trip=request.trip.model_dump(exclude_none=True) if request.trip else None,
        messages=[message.model_dump() for message in request.messages],
    )
    generation = await client.generate_json(
        prompt.SYSTEM_PROMPT,
        user_turn,
        RESPONSE_SCHEMA,
        temperature=prompt.TEMPERATURE,
        timeout=timeout,
        fallback_model=fallback_model,
    )
    if not generation.text:
        raise InvalidModelOutputError("empty response", generation.finish_reason)
    try:
        parsed = ModelAnswer.model_validate_json(generation.text)
    except ValidationError as exc:
        # Log only error locations/types, never the conversation or model output.
        locations = [".".join(map(str, error["loc"])) for error in exc.errors()][:5]
        logger.warning(
            "Assistant output failed validation (finish_reason=%s): %s",
            generation.finish_reason,
            locations,
        )
        raise InvalidModelOutputError("schema-invalid response", generation.finish_reason) from exc
    reply = clean_reply(parsed.reply)
    if not reply:
        raise InvalidModelOutputError("empty reply", generation.finish_reason)
    known_guides = {chunk.id: chunk for chunk in guides}
    return AssistantResponse(
        reply=reply,
        destination=destination,
        places=[
            place.card(language) for place in pick_places(parsed.place_ids, found.places, reply)
        ],
        guides=[
            AssistantGuide(id=gid, title=known_guides[gid].title(language))
            for gid in dict.fromkeys(parsed.guide_ids)
            if gid in known_guides
        ],
        meta=AssistantMeta(
            model=generation.resolved_model or client.model,
            retrieved=len(found.places),
            semantic=found.semantic,
        ),
    )
