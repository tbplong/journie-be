"""Column coverage and data-issue report for a POI Parquet file (spec section 3).

    uv run --group data python scripts/coverage_report.py data/places_100.parquet > report.md

Run it on every new dataset before importing: it confirms the known issues (categories outside
the taxonomy, low-confidence hours, sparse price/description, interest coverage, taxonomy
categories no interest reaches) and lists the cuisine values the keyword map can use.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.retrieval.config import INTEREST_MAP, load_overrides
from app.services.retrieval.ingest import prepare_row, taxonomy_issues
from app.services.retrieval.taxonomy import covers, load_taxonomy

DEFAULT_TAXONOMY = ROOT / "data" / "taxonomy.v1.yaml"
REPORT_COLUMNS = (
    "id", "name", "name_en", "normalized_name_ascii", "kind", "category", "cuisine", "features",
    "michelin", "latitude", "longitude", "ward", "address", "outside_region",
    "opening_hours_raw", "opening_hours_normalized", "opening_hours_confidence",
    "hours_contradicted", "price_raw", "description", "tier", "recommended_by_count",
    "confidence", "recommendable", "website", "phone", "expected_visit_duration_min",
    "cost_typical_vnd",
)


def present(value: Any) -> bool:
    if value is None or value == "" or value == []:
        return False
    if isinstance(value, dict):
        return any(value.values())
    return True


def pct(part: int, whole: int) -> str:
    return f"{part} ({100 * part / whole:.0f}%)" if whole else "0"


def table(title: str, counts: Counter, total: int) -> list[str]:
    lines = [f"\n## {title}\n", "| value | rows |", "|---|---|"]
    lines += [f"| {value} | {pct(n, total)} |" for value, n in counts.most_common()]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    args = parser.parse_args()
    taxonomy = load_taxonomy(args.taxonomy)
    rows = pq.read_table(args.parquet).to_pylist()
    n = len(rows)
    prepared = {p["id"]: p for p in (prepare_row(r, load_overrides(), "report") for r in rows)}
    out = [f"# Coverage report: {args.parquet.name}\n", f"{n} rows\n", "## Column coverage\n"]
    out += ["| column | non-empty |", "|---|---|"]
    out += [f"| {c} | {pct(sum(present(r.get(c)) for r in rows), n)} |" for c in REPORT_COLUMNS]

    out += table("category (after overrides)", Counter(p["category"] for p in prepared.values()), n)
    out += table("tier", Counter(r.get("tier") for r in rows), n)
    out += table("michelin", Counter(r.get("michelin") for r in rows), n)
    out += table("opening_hours_confidence", Counter(r.get("opening_hours_confidence") for r in rows), n)
    out += table("hours_contradicted", Counter(r.get("hours_contradicted") for r in rows), n)
    out += table("cuisine values", Counter(c for r in rows for c in r.get("cuisine") or []), n)
    out += table("features values", Counter(f for r in rows for f in r.get("features") or []), n)
    out += table(
        "description by category",
        Counter(p["category"] for p in prepared.values() if p.get("description")),
        n,
    )

    issues = taxonomy_issues(
        ((r.get("taxonomy_version"), prepared[r["id"]]["category"]) for r in rows), taxonomy
    )
    out.append(f"\n## taxonomy problems ({taxonomy.version})\n")
    out += [f"- {count} rows: {problem}" for problem, count in issues.most_common()] or ["- none"]

    out.append("\n## interest coverage (recommendable, inside region)\n")
    out += ["| group | nodes | POIs |", "|---|---|---|"]
    eligible = [p for p in prepared.values() if p["recommendable"] and not p["outside_region"]]
    for group in INTEREST_MAP.groups.values():
        count = sum(group.node_for(p["category"]) is not None for p in eligible)
        out.append(f"| {group.key} | {', '.join(group.nodes) or '-'} | {count} |")
    used = {node for group in INTEREST_MAP.groups.values() for node in group.nodes}
    unreached = sorted(c for c in taxonomy.categories if not any(covers(u, c) for u in used))
    out.append(f"\n## taxonomy categories no interest reaches ({len(unreached)})\n")
    out += [f"- {category}" for category in unreached]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
