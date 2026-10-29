"""Parquet row -> ``pois`` row: derived search columns and data fixes. Pure, no I/O.

Used by scripts/import_parquet.py; kept in the app package so the rules are unit-tested and
share ``norm()`` with query time.
"""

import json
from collections import Counter
from collections.abc import Iterable
from typing import Any

from app.services.retrieval.scoring import MICHELIN_LABELS
from app.services.retrieval.taxonomy import Taxonomy
from app.services.retrieval.text import fold_case, lexeme_vector, norm


def seed_names(raw_data: str | dict[str, Any] | None) -> list[str]:
    if not raw_data:
        return []
    data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    return [name.strip() for name in (data.get("seed_names") or "").split("|") if name.strip()]


def taxonomy_issues(
    rows: Iterable[tuple[str | None, str | None]], taxonomy: Taxonomy
) -> Counter[str]:
    """Problem -> row count, for (taxonomy_version, category after overrides) pairs."""
    issues: Counter[str] = Counter()
    for version, category in rows:
        if version != taxonomy.version:
            issues[f"taxonomy_version {version!r} is not {taxonomy.version!r}"] += 1
        if category not in taxonomy.categories:
            issues[f"category {category!r} is not in {taxonomy.version}"] += 1
    return issues


def prepare_row(
    row: dict[str, Any], overrides: dict[str, dict[str, str]], data_version: str
) -> dict[str, Any]:
    cuisine = list(row.get("cuisine") or [])
    features = list(row.get("features") or [])
    seeds = seed_names(row.get("raw_data"))
    override = overrides.get(row["id"], {})

    name_keys = [
        key
        for key in dict.fromkeys(
            norm(text)
            for text in (
                row["name"],
                row.get("name_en"),
                row.get("normalized_name"),
                row.get("normalized_name_ascii"),
                *seeds,
            )
        )
        if key
    ]
    # Accent-aware columns use only text as written: the pipeline's ASCII name is already folded
    # and would make "Phố X" look like an unaccented "pho" (see text.py accent rule).
    written_names = [row["name"], row.get("name_en"), *seeds]
    name_keys_vi = [key for key in dict.fromkeys(map(fold_case, written_names)) if key]
    michelin = row.get("michelin")
    search_parts = [
        row["name"],
        row.get("name_en"),
        *seeds,
        *cuisine,
        *features,
        MICHELIN_LABELS.get(michelin or "", ""),
        "michelin" if michelin else "",
        row.get("description"),
    ]
    return {
        "id": row["id"],
        "name": row["name"],
        "name_en": row.get("name_en"),
        "kind": override.get("kind", row["kind"]),
        "category": override.get("category", row.get("category")),
        "cuisine": cuisine,
        "features": features,
        "michelin": michelin,
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "ward": row.get("ward"),
        "address": row.get("address"),
        "city": row.get("city"),
        "outside_region": bool(row.get("outside_region")),
        "opening_hours_raw": row.get("opening_hours_raw"),
        "opening_hours": row.get("opening_hours_normalized"),
        "hours_confidence": row.get("opening_hours_confidence"),
        "hours_contradicted": row.get("hours_contradicted"),
        "price_raw": row.get("price_raw"),
        "description": row.get("description"),
        "tier": row.get("tier"),
        "recommended_by_count": row.get("recommended_by_count"),
        "confidence": row.get("confidence"),
        "recommendable": row.get("recommendable") is not False,
        "website": row.get("website"),
        "phone": row.get("phone"),
        "name_keys": name_keys,
        "name_doc": " ".join(name_keys),
        "search_doc": " ".join(part for part in map(norm, search_parts) if part),
        "name_keys_vi": name_keys_vi,
        "name_text": " ".join(name_keys_vi),
        "name_lex": lexeme_vector(written_names),
        "search_lex": lexeme_vector(search_parts),
        "data_version": data_version,
    }
