import json
from typing import Any

from app.services.retrieval.ingest import prepare_row, seed_names, taxonomy_issues
from app.services.retrieval.taxonomy import Taxonomy


def row(**fields: Any) -> dict[str, Any]:
    return {
        "id": "x",
        "name": "Chợ Bến Thành",
        "name_en": "Ben Thanh Market",
        "normalized_name": "chợ bến thành",
        "normalized_name_ascii": "cho ben thanh",
        "kind": "market",
        "category": "shopping.market",
        "cuisine": None,
        "features": None,
        "michelin": None,
        "raw_data": json.dumps({"seed_names": "Chợ Bến Thành|Ben Thanh Market| Chợ Sài Gòn "}),
        "description": "Khu chợ lâu đời.",
        **fields,
    }


def test_seed_names_split_and_trim() -> None:
    assert seed_names(row()["raw_data"]) == ["Chợ Bến Thành", "Ben Thanh Market", "Chợ Sài Gòn"]
    assert seed_names(None) == []


def test_name_keys_are_normalized_and_deduplicated() -> None:
    prepared = prepare_row(row(), {}, "v1")
    assert prepared["name_keys"] == ["cho ben thanh", "ben thanh market", "cho sai gon"]
    assert prepared["name_doc"] == "cho ben thanh ben thanh market cho sai gon"
    assert "khu cho lau doi" in prepared["search_doc"]


def test_overrides_replace_category_and_kind() -> None:
    overrides = {"x": {"category": "entertainment.nightlife"}}
    prepared = prepare_row(row(kind="cafe", category="food.cafe"), overrides, "v1")
    assert prepared["category"] == "entertainment.nightlife" and prepared["kind"] == "cafe"
    assert prepare_row(row(kind="cafe"), {"x": {"kind": "restaurant"}}, "v1")["kind"] == "restaurant"
    assert prepare_row(row(), {}, "v1")["category"] == "shopping.market"


def test_taxonomy_issues_count_rows_per_problem() -> None:
    taxonomy = Taxonomy("tax/v1", frozenset({"food.cafe", "shopping.market"}))
    rows = [("tax/v1", "food.cafe"), ("tax/v1", "culture.old"), ("tax/v1", "culture.old"),
            ("tax/v0", "shopping.market"), ("tax/v1", None)]
    assert taxonomy_issues(rows, taxonomy) == {
        "category 'culture.old' is not in tax/v1": 2,
        "taxonomy_version 'tax/v0' is not 'tax/v1'": 1,
        "category None is not in tax/v1": 1,
    }
    assert taxonomy_issues([], taxonomy) == {}


def test_michelin_and_cuisine_are_searchable() -> None:
    prepared = prepare_row(row(michelin="bib_gourmand", cuisine=["các_món_ăn_huế"]), {}, "v1")
    assert "michelin bib gourmand" in prepared["search_doc"]
    assert "cac mon an hue" in prepared["search_doc"]


def test_accent_columns_use_only_text_as_written() -> None:
    prepared = prepare_row(row(), {}, "v1")
    assert prepared["name_keys_vi"] == ["chợ bến thành", "ben thanh market", "chợ sài gòn"]
    # The pipeline's ASCII name ("cho ben thanh") must not look like an unaccented spelling:
    # 'cho' only appears folded (weight B), never as written (weight A).
    lexemes = dict(part.split(":", 1) for part in prepared["name_lex"].split())
    assert all(spot.endswith("B") for spot in lexemes["'cho'"].split(","))
    assert all(spot.endswith("A") for spot in lexemes["'chợ'"].split(","))
    assert "'khu'" in prepared["search_lex"]
