from typing import Any

from app.schemas.extraction import Extraction
from app.services.extraction.processing import consistency_issues, repair_extraction

BASE: dict[str, Any] = {
    "destination": None,
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


def test_destination_and_interest_aliases_are_canonicalized() -> None:
    repaired, repairs = repair_extraction(
        extraction(
            destination="Can Tho",
            interests=["cà phê", "cafe"],
            hard_constraints=[atom("destination", "eq", "Can Tho")],
            soft_constraints=[atom("interests", "prefer", "cà phê")],
        )
    )

    assert repaired.destination == "Cần Thơ"
    assert repaired.interests == ["cafe"]
    assert repaired.hard_constraints[0].value == "Cần Thơ"
    assert "canonical_name" in repairs


def test_budget_cap_fills_scalar_and_conflict_sets_clarification() -> None:
    repaired, repairs = repair_extraction(
        extraction(
            hard_constraints=[
                atom("budget_vnd", "min", "5000000"),
                atom("budget_vnd", "max", "3000000"),
            ]
        )
    )

    assert repaired.budget_vnd == 3000000
    assert repaired.possible_conflicts == ["budget_disagreement"]
    assert repaired.needs_clarification is True
    assert repairs[:3] == [
        "scalar_from_constraint",
        "conflict_from_observation",
        "clarification_from_conflict",
    ]


def test_disproved_conflict_is_removed_but_clarification_is_kept() -> None:
    repaired, _ = repair_extraction(
        extraction(
            start_date="2026-11-01",
            end_date="2026-11-03",
            duration_days=3,
            hard_constraints=[
                atom("start_date", "eq", "2026-11-01"),
                atom("end_date", "eq", "2026-11-03"),
                atom("duration_days", "eq", "3"),
            ],
            possible_conflicts=["duration_disagreement"],
            needs_clarification=True,
        )
    )

    assert repaired.possible_conflicts == []
    assert repaired.needs_clarification is True


def test_lists_and_atoms_are_synchronized_and_deduplicated() -> None:
    repaired, repairs = repair_extraction(
        extraction(
            avoid=["Chợ đêm"],
            hard_constraints=[
                atom("must_visit", "include", "Hồ Xuân Hương"),
                atom("must_visit", "include", "Hồ Xuân Hương"),
            ],
        )
    )

    assert repaired.must_visit == ["Hồ Xuân Hương"]
    assert atom("avoid", "exclude", "Chợ đêm") in [
        a.model_dump() for a in repaired.hard_constraints
    ]
    assert len(repaired.hard_constraints) == 2
    assert {"dedupe_constraint", "list_from_constraint", "constraint_from_list"} <= set(repairs)


def test_missing_fields_are_derived_from_null_scalars() -> None:
    repaired, _ = repair_extraction(extraction(available_start="08:00"))

    assert repaired.missing_fields == [
        "destination",
        "travel_dates_or_duration",
        "budget",
        "available_time",
    ]
    assert consistency_issues(repaired)[0]["code"] == "missing_atom"


def test_rejected_extraction_is_left_unchanged() -> None:
    rejected = extraction(
        missing_fields=["destination", "travel_dates_or_duration", "budget", "available_time"]
    )

    repaired, repairs = repair_extraction(rejected)

    assert repaired == rejected
    assert repairs == []
    assert consistency_issues(repaired) == []
