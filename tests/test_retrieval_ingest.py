import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

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


def test_unreadable_hours_are_stored_as_unknown() -> None:
    good = {"mon": [["06:00", "22:00"]]}
    assert prepare_row(row(opening_hours_normalized=good), {}, "v1")["opening_hours"] == good
    bad_values = (
        {"mon": [["9h00", "22:00"]]},
        {"mon": [["06:00", "25:00"]]},
        {"mon": "06-22"},
        {"mon": [["12:00", "12:75"]]},  # minutes past 59 used to read as 13:15
        {"mon": [["08:00", "1_7:00"]]},  # int() accepts digit separators
        {"mon": [[" 8:00", "17:00"]]},
        {"Mon": [["08:00", "17:00"]]},  # an unknown day key read as "hours unknown"
    )
    for bad in bad_values:
        assert prepare_row(row(opening_hours_normalized=bad), {}, "v1")["opening_hours"] is None
    assert prepare_row(row(), {}, "v1")["opening_hours"] is None


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
    lexemes = {part.split(":", 1)[0] for part in prepared["search_lex"].split()}
    assert {"'michelin'", "'bib'", "'gourmand'", "'huế'", "'hue'"} <= lexemes


def test_accent_columns_use_only_text_as_written() -> None:
    prepared = prepare_row(row(), {}, "v1")
    assert prepared["name_keys_vi"] == ["chợ bến thành", "ben thanh market", "chợ sài gòn"]
    # The pipeline's ASCII name ("cho ben thanh") must not look like an unaccented spelling:
    # 'cho' only appears folded (weight B), never as written (weight A).
    lexemes = dict(part.split(":", 1) for part in prepared["name_lex"].split())
    assert all(spot.endswith("B") for spot in lexemes["'cho'"].split(","))
    assert all(spot.endswith("A") for spot in lexemes["'chợ'"].split(","))
    assert "'khu'" in prepared["search_lex"]


def load_import_script() -> Any:
    pytest.importorskip("pyarrow")  # dependency group "data"
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_parquet.py"
    spec = importlib.util.spec_from_file_location("import_parquet", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoredRows:
    """Stands in for a psycopg connection: answers the one SELECT of description_updates."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows, self.queries = rows, []

    def execute(self, query: Any, params: Any = None) -> "StoredRows":
        self.queries.append(query.as_string(None) if hasattr(query, "as_string") else query)
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_only_description_updates_existing_rows_with_stored_search_inputs() -> None:
    script = load_import_script()
    # id, description, name, name_en, cuisine, features, michelin (as stored, edited by hand)
    stored = StoredRows(
        [
            ("x", "Khu chợ lâu đời.", "Chợ Bến Thành", "Ben Thanh Market", ["seafood"], [], None),
            ("y", "Quán bia hơi.", "Bia Hơi Hà Nội", None, [], [], None),
        ]
    )
    file_rows = [
        row(description="Chợ lâu đời bán hải sản và đồ lưu niệm."),
        row(id="y", name="Bia Hơi Hà Nội", description="Quán bia hơi."),  # unchanged
        row(id="z", description="Không có trong bảng."),  # not in pois: never inserted
    ]
    found, updates = script.description_updates(stored, file_rows, {}, "v1")
    assert found == 2
    assert [set(update) for update in updates] == [{"id", "description", "search_lex"}]
    assert updates[0]["id"] == "x"
    assert updates[0]["description"] == "Chợ lâu đời bán hải sản và đồ lưu niệm."
    lexemes = {part.split(":", 1)[0] for part in updates[0]["search_lex"].split()}
    assert {"'seafood'", "'lưu'", "'niệm'"} <= lexemes  # stored cuisine kept, new words added
    assert script.DESCRIPTION_UPDATE.startswith("UPDATE pois SET description = ")
    assert "INSERT" not in script.DESCRIPTION_UPDATE
    assert all(query.startswith("SELECT") for query in stored.queries)


def test_only_refuses_prune() -> None:
    script = load_import_script()
    with pytest.raises(SystemExit) as stop:
        script.main(["places.parquet", "--only", "description", "--prune"])
    assert stop.value.code == 2
