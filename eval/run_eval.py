"""Retrieval eval on hand-written gold extractions (spec section 8).

    uv run python eval/run_eval.py [--schema retrieval_test] [--cases eval/queries.jsonl] [-v]
        [--no-semantic]

Gold extractions are written by hand, so failures here are retrieval failures, not LLM ones.
Reports name-resolution accuracy, Precision@5 and nDCG@5 per section, the share of evaluated
sections with a relevant item in the top 5, and must_not_contain violations (target: 0; the
exit code is 1 when any occur). Diversity per non-empty section's first page (top 6): the
share of famous places (tier A or Michelin) and the number of distinct wards. The seed is
fixed, so runs are comparable. Free-text interests are embedded with Gemini (one call per such
case, GEMINI_API_KEY then GEMINI_API_KEY2); --no-semantic runs the full-text-only baseline.

The destination of a case is extraction.destination (default Ho Chi Minh City); names are looked
up in that city. Per-case checks on the sections of the response (a "section" is a key or label;
without one, "min" holds when some section satisfies it and "max" when every section does):
  min_total          {"section", "min", "max"}  total_matches of the section(s)
  top_category       {"section", "k", "category" (str or list), "min", "max"}  places of those
                     categories among a section's first k items
  include_top        {"section", "k", "ids", "min", "max"}  the listed ids among the first k
  top_reason         {"section", "k", "contains" (str or list), "min"}  first k items whose
                     reasons hold the text
  max_reason         {"contains", "max", "in_description"}  cards with that reason (at most
                     max); with in_description each of them must have the word in its description
  no_section_like    str or list: no section key or label holds these words, in this order
  matched_keywords   {"section", "contains"}
  max_warnings       int
  no_warning_like    str or list: no warning holds these texts ("Không tìm thấy": a false
                     not-found next to the group's own places)
  no_note            true: no section carries a note (an empty required section shows one)
Each may also be a list of such specs. "no_semantic": true runs the case without the embedder.
--similarities prints the 40 nearest places (cosine similarity, category) of every free-text
request instead of running the eval.
"""

import argparse
import asyncio
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from psycopg import AsyncConnection

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.schemas.extraction import Extraction
from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.extraction.processing import standard_name
from app.services.gemini import embed_all
from app.services.retrieval import candidates, names
from app.services.retrieval.db import reuse
from app.services.retrieval.interests import plan_interests
from app.services.retrieval.poi import DEFAULT_CITY
from app.services.retrieval.semantic import (
    QUERY_FORMAT,
    SEMANTIC_SQL,
    Embed,
    negated,
    vector_literal,
)
from app.services.retrieval.service import DEFAULT_DESTINATION, SUPPORTED_DESTINATIONS, retrieve

BASE: dict[str, Any] = {
    "destination": "Ho Chi Minh City",
    "start_date": None,
    "end_date": None,
    "duration_days": None,
    "budget_vnd": None,
    "budget_scope": None,
    "available_start": None,
    "available_end": None,
    "interests": [],
    "must_visit": [],
    "avoid": [],
    "preferred_places": [],
    "hard_constraints": [],
    "soft_constraints": [],
    "missing_fields": [],
    "possible_conflicts": [],
    "needs_clarification": False,
}
K = 5
PAGE = 6  # default RetrievalRequest.limit
SEED = 0


def ndcg(ranked: list[str], relevant: dict[str, int]) -> float:
    dcg = sum((2 ** relevant.get(i, 0) - 1) / math.log2(r + 2) for r, i in enumerate(ranked[:K]))
    ideal = sorted(relevant.values(), reverse=True)[:K]
    idcg = sum((2**g - 1) / math.log2(r + 2) for r, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn").replace("đ", "d")


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", fold_text(text))


def has_words(haystack: str, needle: str) -> bool:
    """The needle's words appear in the haystack, in order and next to each other."""
    h, n = words(haystack), words(needle)
    return bool(n) and any(h[i : i + len(n)] == n for i in range(len(h) - len(n) + 1))


def specs(case: dict[str, Any], name: str) -> list[Any]:
    value = case.get(name)
    return [] if value is None else value if isinstance(value, list) else [value]


def scope(response: RetrievalResponse, spec: dict[str, Any]) -> list[Any]:
    want = spec.get("section")
    return [s for s in response.sections if want is None or want in (s.key, s.label)]


def judge(counts: list[int], spec: dict[str, Any], name: str, errors: list[str]) -> None:
    """min: some section reaches it (no section in scope fails); max: every section stays under."""
    if "min" in spec and not any(c >= spec["min"] for c in counts):
        errors.append(f"{name} {spec}: best {max(counts, default=None)} < {spec['min']}")
    if "max" in spec and any(c > spec["max"] for c in counts):
        errors.append(f"{name} {spec}: {max(counts)} > {spec['max']}")


def section_checks(
    case: dict[str, Any], response: RetrievalResponse, descriptions: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    for spec in specs(case, "min_total"):
        judge([s.total_matches for s in scope(response, spec)], spec, "min_total", errors)
    for spec in specs(case, "top_category"):
        wanted = set(specs(spec, "category"))
        counts = [sum(i.category in wanted for i in s.items[: spec["k"]]) for s in scope(response, spec)]
        judge(counts, spec, "top_category", errors)
    for spec in specs(case, "include_top"):
        wanted = set(spec["ids"])
        counts = [sum(i.id in wanted for i in s.items[: spec["k"]]) for s in scope(response, spec)]
        judge(counts, spec, "include_top", errors)
    for spec in specs(case, "top_reason"):
        texts = specs(spec, "contains")
        counts = [
            sum(any(t in r for r in i.reasons for t in texts) for i in s.items[: spec["k"]])
            for s in scope(response, spec)
        ]
        judge(counts, spec, "top_reason", errors)
    for spec in specs(case, "max_reason"):
        cards = {
            i.id: i for s in response.sections for i in s.items if any(spec["contains"] in r for r in i.reasons)
        }
        if "max" in spec and len(cards) > spec["max"]:
            errors.append(f"max_reason {spec}: {len(cards)} cards {sorted(cards)}")
        if word := spec.get("in_description"):
            lacking = sorted(i for i in cards if word not in (descriptions.get(i) or "").lower())
            if lacking:
                errors.append(f"max_reason {spec}: description lacks the word on {lacking}")
    for needle in specs(case, "no_section_like"):
        found = [s.key for s in response.sections if has_words(s.key, needle) or has_words(s.label, needle)]
        if found:
            errors.append(f"no_section_like {needle!r}: sections {found}")
    for spec in specs(case, "matched_keywords"):
        got = [k for s in scope(response, spec) for k in s.matched_keywords]
        if spec["contains"] not in got:
            errors.append(f"matched_keywords {spec}: {got}")
    if (limit := case.get("max_warnings")) is not None and len(response.warnings) > limit:
        errors.append(f"{len(response.warnings)} warnings > {limit}: {response.warnings}")
    for needle in specs(case, "no_warning_like"):
        if found := [w for w in response.warnings if needle in w]:
            errors.append(f"no_warning_like {needle!r}: {found}")
    if case.get("no_note") and (noted := [(s.key, s.note) for s in response.sections if s.note]):
        errors.append(f"no_note: {noted}")
    return errors


def city_of(extraction: Extraction) -> str:
    destination = (
        standard_name("destination", extraction.destination) if extraction.destination else None
    )
    return SUPPORTED_DESTINATIONS.get(destination or DEFAULT_DESTINATION, DEFAULT_CITY)


async def run_case(
    conn: AsyncConnection, case: dict[str, Any], embed: Embed | None = None
) -> dict[str, Any]:
    extraction = Extraction.model_validate({**BASE, **case["extraction"]})
    # Every item of every section, so must_not_contain sees past the first page.
    request = RetrievalRequest.model_construct(
        extraction=extraction, selected_ids=[], section=None, offset=0, limit=10_000, seed=SEED
    )
    if case.get("no_semantic"):
        embed = None
    response: RetrievalResponse = await retrieve(reuse(conn), request, embed)
    errors: list[str] = []
    result: dict[str, Any] = {"id": case["id"], "sections": {}, "names": []}

    if (status := case.get("expect_status", "ok")) != response.status:
        errors.append(f"status {response.status} != {status}")
    if (warning := case.get("expect_warning")) and not any(warning in w for w in response.warnings):
        errors.append(f"no warning mentioning {warning!r}: {response.warnings}")
    if expected := case.get("expect_sections"):
        keys = [s.key for s in response.sections]
        if keys != expected:
            errors.append(f"sections {keys} != {expected}")

    for spec in case.get("names", []):
        match = await names.lookup(conn, spec["query"], city_of(extraction))
        got_id = match.hits[0].poi_id if match.status == "resolved" else None
        ok = match.status == spec["status"] and (spec["id"] is None or got_id == spec["id"])
        result["names"].append(ok)
        if not ok:
            errors.append(f"name {spec['query']!r}: {match.status} {got_id} != {spec['status']} {spec['id']}")

    descriptions: dict[str, str] = {}
    if any("in_description" in spec for spec in specs(case, "max_reason")):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, description FROM pois WHERE id = ANY(%s)",
                ([i.id for s in response.sections for i in s.items],),
            )
            descriptions = {row[0]: row[1] for row in await cur.fetchall()}
    errors.extend(section_checks(case, response, descriptions))
    listed = {item.id for s in response.sections for item in s.items}
    violations = sorted(listed & set(case.get("must_not_contain", [])))
    if violations:
        errors.append(f"must_not_contain present: {violations}")
    # A place may be listed but must not be *explained* by a word, e.g. a museum under
    # "Văn hoá" with the reason "Khớp “phở”". Rule: {"contains": text, "ids": [...]} and/or
    # {"contains": text, "outside_sections": [...]} (the reason is allowed only there).
    for rule in case.get("forbidden_reasons", []):
        allowed = rule.get("outside_sections")
        bad = sorted(
            item.id
            for section in response.sections
            for item in section.items
            if any(rule["contains"] in reason for reason in item.reasons)
            and (item.id in rule.get("ids", []) or (allowed is not None and section.key not in allowed))
        )
        if bad:
            errors.append(f"reason containing {rule['contains']!r} on {bad}")
            violations.extend(f"{item_id} (reason)" for item_id in bad)
    result["violations"] = violations

    relevant: dict[str, int] = case.get("relevant", {})
    if relevant:
        wanted = case.get("eval_sections")
        for section in response.sections:
            if wanted and section.key not in wanted:
                continue
            top = [item.id for item in section.items[:K]]
            # Only sections that contain at least one labelled id are judged.
            if not any(i in relevant for i in (item.id for item in section.items)):
                continue
            hits = sum(relevant.get(i, 0) >= 1 for i in top)
            section_relevant = {i: g for i, g in relevant.items() if i in {x.id for x in section.items}}
            result["sections"][section.key] = {
                "p_at_5": hits / len(top) if top else 0.0,
                "ndcg_at_5": ndcg(top, section_relevant),
                "hit": hits > 0,
                "top": top,
            }
    for key, expected_id in case.get("top1", {}).items():
        section = next((s for s in response.sections if s.key == key), None)
        first = section.items[0].id if section and section.items else None
        if first != expected_id:
            errors.append(f"top1[{key}] {first} != {expected_id}")
    pages = [[item.id for item in s.items[:PAGE]] for s in response.sections if s.items]
    pois = await candidates.fetch_by_ids(conn, [i for page in pages for i in page])
    result["diversity"] = [
        (
            sum(pois[i].tier == "A" or pois[i].michelin is not None for i in page) / len(page),
            len({pois[i].ward for i in page}),
        )
        for page in pages
    ]
    result["errors"] = errors
    return result


async def dump_similarities(
    conn: AsyncConnection, cases: list[dict[str, Any]], embed: Embed | None
) -> None:
    """The 40 nearest eligible places to every free-text request, whatever the threshold: to pick
    SIMILARITY_MIN and to see what a query lands next to. Read-only."""
    if embed is None:
        raise SystemExit("--similarities needs a Gemini key")
    for case in cases:
        extraction = Extraction.model_validate({**BASE, **case["extraction"]})
        city = city_of(extraction)
        cuisines, _ = await candidates.data_info(conn, city)
        plan = plan_interests(extraction, cuisines)
        params = candidates.eligibility(plan.avoid, set(), city)
        for req in (r for r in plan.requests if r.text is not None):
            [vector] = await embed([QUERY_FORMAT.format(req.raw)])
            async with conn.cursor() as cur:
                await cur.execute(
                    SEMANTIC_SQL,
                    {**params, "vector": vector_literal(vector), "k": 40, "min": 0.0},
                )
                found = sorted(await cur.fetchall(), key=lambda row: -row[1])
                await cur.execute(
                    "SELECT id, category, name FROM pois WHERE id = ANY(%s)", ([r[0] for r in found],)
                )
                info = {row[0]: row[1:] for row in await cur.fetchall()}
            flag = " (negated: not searched)" if negated(req.raw) else ""
            print(f"\n## {case['id']} [{city}] {req.raw!r}{flag}")
            for poi_id, similarity in found:
                category, name = info[poi_id]
                print(f"{similarity:.4f}  {category or '-':<28} {poi_id}  {name}")


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", default="retrieval_test")
    parser.add_argument("--cases", type=Path, default=ROOT / "eval" / "queries.jsonl")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print each section's top 5")
    parser.add_argument("--no-semantic", action="store_true", help="Full-text-only baseline")
    parser.add_argument(
        "--similarities", action="store_true",
        help="Print the 40 nearest places (cosine similarity) of every free-text request; no eval",
    )
    parser.add_argument("--only", help="Comma-separated case ids (e.g. to run the cases in halves)")
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("DATABASE_URL is not set")
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]

    conn = await AsyncConnection.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        options=f"-c search_path={args.schema},public",
    )
    keys = [k.get_secret_value() for k in (settings.gemini_api_key, settings.gemini_api_key2) if k]
    async with conn, httpx.AsyncClient() as http:

        async def embed(texts: list[str]) -> list[list[float]]:
            return await embed_all(http, settings, texts, keys)

        use = None if args.no_semantic or not keys else embed
        if args.similarities:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            await dump_similarities(conn, cases, use)
            return 0
        results = [await run_case(conn, case, use) for case in cases]

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    for result in results:
        mark = "FAIL" if result["errors"] else "ok  "
        print(f"{mark} {result['id']}")
        for error in result["errors"]:
            print(f"       {error}")
        for key, section in result["sections"].items():
            if args.verbose or section["p_at_5"] < 0.6:
                print(f"       {key}: P@5={section['p_at_5']:.2f} nDCG@5={section['ndcg_at_5']:.2f} "
                      f"top={section['top']}")

    name_checks = [ok for r in results for ok in r["names"]]
    sections = [s for r in results for s in r["sections"].values()]
    violations = sum(len(r["violations"]) for r in results)
    print("\n| metric | value |\n|---|---|")
    print(f"| cases passing all checks | {sum(not r['errors'] for r in results)}/{len(results)} |")
    print(f"| name resolution accuracy | {sum(name_checks)}/{len(name_checks)} |")
    print(f"| mean P@5 ({len(sections)} sections) | {mean([s['p_at_5'] for s in sections]):.3f} |")
    print(f"| mean nDCG@5 | {mean([s['ndcg_at_5'] for s in sections]):.3f} |")
    print(f"| sections with a relevant top-5 item | {mean([s['hit'] for s in sections]):.3f} |")
    pages = [d for r in results for d in r["diversity"]]
    print(f"| famous share of top {PAGE} ({len(pages)} sections) | {mean([d[0] for d in pages]):.3f} |")
    print(f"| distinct wards in top {PAGE} | {mean([d[1] for d in pages]):.2f} |")
    print(f"| must_not_contain violations | {violations} |")
    return 1 if violations else 0


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(main(), loop_factory=loop_factory))
