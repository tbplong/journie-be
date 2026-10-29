"""Retrieval eval on hand-written gold extractions (spec section 8).

    uv run python eval/run_eval.py [--schema retrieval_test] [--cases eval/queries.jsonl] [-v]

Gold extractions are written by hand, so failures here are retrieval failures, not LLM ones.
Reports name-resolution accuracy, Precision@5 and nDCG@5 per section, the share of evaluated
sections with a relevant item in the top 5, and must_not_contain violations (target: 0; the
exit code is 1 when any occur).
"""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.schemas.extraction import Extraction
from app.schemas.retrieval import RetrievalRequest, RetrievalResponse
from app.services.retrieval import names
from app.services.retrieval.service import retrieve

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


def ndcg(ranked: list[str], relevant: dict[str, int]) -> float:
    dcg = sum((2 ** relevant.get(i, 0) - 1) / math.log2(r + 2) for r, i in enumerate(ranked[:K]))
    ideal = sorted(relevant.values(), reverse=True)[:K]
    idcg = sum((2**g - 1) / math.log2(r + 2) for r, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


async def run_case(conn: AsyncConnection, case: dict[str, Any]) -> dict[str, Any]:
    extraction = Extraction.model_validate({**BASE, **case["extraction"]})
    # Every item of every section, so must_not_contain sees past the first page.
    request = RetrievalRequest.model_construct(
        extraction=extraction, selected_ids=[], section=None, offset=0, limit=10_000
    )
    response: RetrievalResponse = await retrieve(conn, request)
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
        match = await names.lookup(conn, spec["query"])
        got_id = match.hits[0].poi_id if match.status == "resolved" else None
        ok = match.status == spec["status"] and (spec["id"] is None or got_id == spec["id"])
        result["names"].append(ok)
        if not ok:
            errors.append(f"name {spec['query']!r}: {match.status} {got_id} != {spec['status']} {spec['id']}")

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
    result["errors"] = errors
    return result


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", default="retrieval_test")
    parser.add_argument("--cases", type=Path, default=ROOT / "eval" / "queries.jsonl")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print each section's top 5")
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("DATABASE_URL is not set")
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line]

    conn = await AsyncConnection.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        options=f"-c search_path={args.schema},public",
    )
    async with conn:
        results = [await run_case(conn, case) for case in cases]

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
    print(f"| must_not_contain violations | {violations} |")
    return 1 if violations else 0


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(main(), loop_factory=loop_factory))
