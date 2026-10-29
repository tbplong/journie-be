"""The interest map, overrides and taxonomy file must agree (data/taxonomy.v1.yaml)."""

import csv
from pathlib import Path

from app.services.retrieval.config import CONFIG_DIR, INTEREST_MAP, Group
from app.services.retrieval.taxonomy import covers, depth, load_taxonomy, top

TAXONOMY = load_taxonomy(Path(__file__).resolve().parents[1] / "data" / "taxonomy.v1.yaml")


def test_node_helpers() -> None:
    assert top("food.cafe") == "food" and top("food") == "food"
    assert depth("food") == 1 and depth("food.cafe") == 2
    assert covers("food", "food.cafe") and covers("food.cafe", "food.cafe")
    assert not covers("food", "foodcourt.x") and not covers("food.cafe", "food")
    assert not covers("food", None) and not covers("food", "")


def test_group_picks_its_deepest_covering_node() -> None:
    group = Group("g", "G", ("food", "food.cafe"))
    assert group.node_for("food.cafe") == "food.cafe"
    assert group.node_for("food.restaurant") == "food"
    assert group.node_for("nature.park") is None


def test_interest_nodes_exist_in_the_taxonomy_or_are_declared_gaps() -> None:
    assert INTEREST_MAP.taxonomy == TAXONOMY.version
    used = {node for group in INTEREST_MAP.groups.values() for node in group.nodes}
    assert used - TAXONOMY.nodes == set(INTEREST_MAP.gaps), "undeclared or unused taxonomy gap"
    assert not set(INTEREST_MAP.gaps) & TAXONOMY.nodes, "gap is in the taxonomy now: remove it"


def test_category_overrides_use_taxonomy_values() -> None:
    with (CONFIG_DIR / "poi_overrides.csv").open(encoding="utf-8", newline="") as handle:
        values = {row["value"] for row in csv.DictReader(handle) if row["field"] == "category"}
    assert values <= TAXONOMY.categories
