from typing import Any

from app.schemas.extraction import Extraction
from app.services.retrieval.config import INTEREST_MAP
from app.services.retrieval.interests import plan_interests
from app.services.retrieval.names import NameHit, classify
from app.services.retrieval.text import norm

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


def extraction(**fields: Any) -> Extraction:
    return Extraction.model_validate({**BASE, **fields})


def atom(field: str, operator: str, value: str) -> dict[str, str]:
    return {"field": field, "operator": operator, "value": value}


def test_norm_strips_accents_case_and_punctuation() -> None:
    assert norm("Phở  Hùng!") == "pho hung"
    assert norm("Đường Đồng Khởi") == "duong dong khoi"
    assert norm("các_món_ăn_huế") == "cac mon an hue"
    assert norm("Bar & Kitchen") == "bar kitchen"
    assert norm(None) == ""


def test_single_exact_hit_resolves() -> None:
    match = classify("Chợ Bến Thành", [NameHit("a", 1.0, True), NameHit("b", 0.7, False)])
    assert match.status == "resolved"
    assert [hit.poi_id for hit in match.hits] == ["a"]


def test_several_exact_hits_are_ambiguous() -> None:
    match = classify("Phở Hùng", [NameHit("a", 1.0, True), NameHit("b", 1.0, True)])
    assert match.status == "ambiguous"
    assert len(match.hits) == 2


def test_clear_fuzzy_winner_resolves_but_close_runner_up_is_ambiguous() -> None:
    assert classify("cho ben tanh", [NameHit("a", 0.72, False), NameHit("b", 0.4, False)]).status == (
        "resolved"
    )
    close = classify("pho", [NameHit("a", 0.9, False), NameHit("b", 0.85, False),
                             NameHit("c", 0.5, False)])
    assert close.status == "ambiguous"
    assert [hit.poi_id for hit in close.hits] == ["a", "b"]


def test_weak_match_is_not_found_and_never_substituted() -> None:
    assert classify("Landmark 81", [NameHit("a", 0.3, False)]).status == "not_found"
    assert classify("Landmark 81", []).hits == ()


def test_standard_interests_with_strength() -> None:
    plan = plan_interests(
        extraction(
            interests=["museum", "food"],
            hard_constraints=[atom("interests", "include", "museum")],
            soft_constraints=[atom("interests", "prefer", "food")],
        )
    )
    assert [(r.group, r.strength) for r in plan.requests] == [
        ("museum", "include"),
        ("food", "prefer"),
    ]


def test_non_standard_interests_map_to_keywords_groups_or_text() -> None:
    plan = plan_interests(
        extraction(interests=["Sao Michelin", "ăn chay", "bar", "phở", "ramen"]),
        cuisines={"ramen": "ramen"},
    )
    by_raw = {r.raw: r for r in plan.requests}
    assert by_raw["Sao Michelin"].keyword is not None
    assert by_raw["Sao Michelin"].keyword.filter == {"michelin": ("star",)}
    assert by_raw["ăn chay"].keyword is not None
    assert by_raw["ăn chay"].keyword.key == "vegetarian"
    assert by_raw["bar"].group == "nightlife"
    assert by_raw["phở"].text == "pho"
    assert by_raw["ramen"].keyword is not None


def test_other_atoms_become_requests_and_duplicates_merge() -> None:
    plan = plan_interests(
        extraction(
            interests=["cafe"],
            hard_constraints=[atom("other", "include", "cà phê"), atom("other", "eq", "michelin")],
            soft_constraints=[atom("interests", "prefer", "cafe")],
        )
    )
    assert [(r.key, r.strength) for r in plan.requests] == [
        ("cafe", "include"),
        ("kw:michelin", "include"),
    ]


def test_avoid_splits_into_nodes_filters_and_names() -> None:
    plan = plan_interests(extraction(avoid=["bảo tàng", "bar", "michelin", "Chợ Bến Thành"]))
    groups = INTEREST_MAP.groups
    assert plan.avoid.nodes == {*groups["museum"].nodes, *groups["nightlife"].nodes}
    assert [k.key for k in plan.avoid.filters] == ["michelin"]
    assert plan.avoid.names == ("Chợ Bến Thành",)
