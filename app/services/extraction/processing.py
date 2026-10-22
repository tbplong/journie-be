"""Deterministic post-processing (``repair-v3``) and read-only checks (``consistency-v2``).

Ported from journie-data ``src/journie-llm/processing.py``. Repairs use only the model
output's own values: they never read the user text, never invent a value, and never lower
``needs_clarification``.
"""

import csv
import json
import unicodedata
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.extraction import Extraction

PROCESSING_VERSION = "repair-v3"
DIAGNOSTICS_VERSION = "consistency-v2"

MISSING_FIELD_RULES: dict[str, tuple[tuple[str, ...], Callable[[Iterable[bool]], bool]]] = {
    "destination": (("destination",), all),
    "travel_dates_or_duration": (("start_date", "end_date", "duration_days"), all),
    "budget": (("budget_vnd",), all),
    "available_time": (("available_start", "available_end"), any),
}

LIST_ATOM_RULES = {
    "interests": ("soft_constraints", "prefer"),
    "preferred_places": ("soft_constraints", "prefer"),
    "must_visit": ("hard_constraints", "include"),
    "avoid": ("hard_constraints", "exclude"),
}
# semantic-v3: an interest the user states as mandatory is hard include.
LIST_ATOM_ALTERNATIVES = {"interests": ("hard_constraints", "include")}

SCALAR_ATOM_OPERATORS = {
    "destination": ("eq",),
    "start_date": ("eq",),
    "end_date": ("eq",),
    "duration_days": ("eq",),
    "budget_vnd": ("eq", "max"),
    "available_start": ("eq",),
    "available_end": ("eq",),
}


def canonical(value: object) -> str:
    """Comparison key that ignores list order but keeps multiplicity."""
    if isinstance(value, list):
        return json.dumps(sorted(canonical(item) for item in value), ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(
            {key: canonical(item) for key, item in sorted(value.items())},
            ensure_ascii=False,
            sort_keys=True,
        )
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_missing_fields(extraction: Extraction) -> list[str]:
    return [
        label
        for label, (fields, is_missing) in MISSING_FIELD_RULES.items()
        if is_missing(getattr(extraction, field) is None for field in fields)
    ]


def place_key(name: str) -> str:
    """Match key: Unicode NFC, case-insensitive, ignoring spaces, dots, hyphens, underscores.
    Accents are kept; unaccented spellings are listed explicitly in the alias table."""
    key = unicodedata.normalize("NFC", name).casefold()
    return "".join(char for char in key if char not in " .-_\t")


def load_name_aliases(path: Path) -> dict[str, dict[str, str]]:
    """kind ("destination"/"interest") -> alias key -> canonical name, from kind,canonical,alias."""
    aliases: dict[str, dict[str, str]] = {"destination": {}, "interest": {}}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            table, standard = aliases[row["kind"]], row["canonical"]
            for name in (standard, row["alias"]):
                if table.setdefault(place_key(name), standard) != standard:
                    raise ValueError(f"Alias {name!r} maps to two canonical names")
    return aliases


NAME_ALIASES = load_name_aliases(Path(__file__).with_name("name_aliases.csv"))
# Atom/list fields whose values are names from each alias table.
ALIAS_FIELDS = {"destination": "destination", "interests": "interest"}


def standard_name(field: str, value: str) -> str:
    return NAME_ALIASES[ALIAS_FIELDS[field]].get(place_key(value), value)


def list_rules(field: str) -> tuple[tuple[str, str], ...]:
    """Allowed (constraint list, operator) pairs for a list field."""
    alternative = LIST_ATOM_ALTERNATIVES.get(field)
    return (LIST_ATOM_RULES[field], *([alternative] if alternative else []))


def scalar_candidates(extraction: Extraction, field: str) -> set[str]:
    """Constraint values that state a scalar; a budget cap outranks an exact budget."""
    atoms = [atom for atom in extraction.hard_constraints if atom.field == field]
    if field == "budget_vnd" and any(atom.operator == "max" for atom in atoms):
        return {atom.value for atom in atoms if atom.operator == "max"}
    return {atom.value for atom in atoms if atom.operator in SCALAR_ATOM_OPERATORS[field]}


def canonical_int(value: str) -> bool:
    return value.isascii() and value.isdigit() and str(int(value)) == value


def observed_conflicts(extraction: Extraction) -> dict[str, bool | None]:
    """Conflicts the output's own values prove (True) or disprove (False); None = unknown."""
    conflicts: dict[str, bool | None] = {
        "date_order": None,
        "duration_disagreement": None,
        "budget_disagreement": None,
        "must_visit_avoid": bool(set(extraction.must_visit) & set(extraction.avoid)),
    }
    if extraction.start_date is not None and extraction.end_date is not None:
        days = (
            date.fromisoformat(extraction.end_date) - date.fromisoformat(extraction.start_date)
        ).days + 1
        conflicts["date_order"] = days < 1
        if extraction.duration_days is not None:
            conflicts["duration_disagreement"] = extraction.duration_days != days
    bounds: dict[str, list[int]] = {"min": [], "max": []}
    uncertain = False
    for atom in extraction.hard_constraints:
        if atom.field == "budget_vnd" and atom.operator in bounds:
            if canonical_int(atom.value):
                bounds[atom.operator].append(int(atom.value))
            else:
                uncertain = True
    observed = any(minimum > maximum for minimum in bounds["min"] for maximum in bounds["max"])
    # An unreadable bound leaves "no conflict" unproven, but not an observed conflict.
    conflicts["budget_disagreement"] = True if observed else None if uncertain else False
    return conflicts


def _canonicalize_names(data: dict[str, Any], repairs: list[str]) -> None:
    """Map destination and interest aliases to canonical names, so later steps see one spelling."""
    name = data["destination"]
    if name is not None and standard_name("destination", name) != name:
        data["destination"] = standard_name("destination", name)
        repairs.append("canonical_name")
    names = data["interests"]
    data["interests"] = list(dict.fromkeys(standard_name("interests", v) for v in names))
    if data["interests"] != names:
        repairs.append("canonical_name")
    for atom in data["hard_constraints"] + data["soft_constraints"]:
        if atom["field"] in ALIAS_FIELDS:
            standard = standard_name(atom["field"], atom["value"])
            if standard != atom["value"]:
                atom["value"] = standard
                repairs.append("canonical_name")


def repair_extraction(extraction: Extraction) -> tuple[Extraction, list[str]]:
    """Resolve contradictions inside one output using only that output's values.

    Anything with more than one plausible fix (e.g. hard prefer interests) is left for
    diagnostics.

    Returns:
        The repaired extraction and the repair step names applied, in order.
    """
    data = extraction.model_dump()
    repairs: list[str] = []
    _canonicalize_names(data, repairs)

    for kind in ("hard_constraints", "soft_constraints"):
        # Only exact repeats within one list; a hard/soft pair has no single right side.
        seen: set[str] = set()
        kept = []
        for atom in data[kind]:
            key = canonical(atom)
            if key in seen:
                repairs.append("dedupe_constraint")
                continue
            seen.add(key)
            kept.append(atom)
        data[kind] = kept

    for field, (kind, operator) in LIST_ATOM_RULES.items():
        rules = list_rules(field)
        atom_values = [
            atom["value"]
            for kind_name, op in rules
            for atom in data[kind_name]
            if atom["field"] == field and atom["operator"] == op
        ]
        for value in dict.fromkeys(atom_values):
            if value not in data[field]:
                data[field].append(value)
                repairs.append("list_from_constraint")
        # Interest strength is unknown here, so no interest atom is added. A value that
        # already has a misplaced atom (e.g. hard prefer) is left for diagnostics.
        placed = {
            atom["value"]
            for kind_name in ("hard_constraints", "soft_constraints")
            for atom in data[kind_name]
            if atom["field"] == field
        }
        if len(rules) == 1:
            for value in data[field]:
                if value not in placed:
                    data[kind].append({"field": field, "operator": operator, "value": value})
                    repairs.append("constraint_from_list")

    current = Extraction.model_validate(data)
    for field in SCALAR_ATOM_OPERATORS:
        candidates = scalar_candidates(current, field)
        if len(candidates) != 1:
            continue
        stated = candidates.pop()
        scalar: str | int = stated
        if field in ("duration_days", "budget_vnd"):
            if not canonical_int(stated):
                continue
            scalar = int(stated)
        if data[field] == scalar:
            continue
        try:
            Extraction.model_validate({**data, field: scalar})
        except ValidationError:
            continue
        data[field] = scalar
        repairs.append("scalar_from_constraint")

    current = Extraction.model_validate(data)
    declared = data["possible_conflicts"]
    for conflict, present in observed_conflicts(current).items():
        if present is True and conflict not in declared:
            declared.append(conflict)
            repairs.append("conflict_from_observation")
        elif present is False and conflict in declared:
            declared.remove(conflict)
            repairs.append("conflict_from_observation")
    if declared and not data["needs_clarification"]:
        data["needs_clarification"] = True
        repairs.append("clarification_from_conflict")

    repaired = Extraction.model_validate(data)
    missing = derive_missing_fields(repaired)
    if missing != repaired.missing_fields:
        repairs.append("missing_fields")
    return repaired.model_copy(update={"missing_fields": missing}), repairs


def consistency_issues(extraction: Extraction) -> list[dict[str, str]]:
    """Check representation consistency, not grounding, feasibility, or user intent."""
    issues: list[dict[str, str]] = []

    def report(code: str, field: str, detail: str, severity: str = "error") -> None:
        issues.append({"code": code, "field": field, "severity": severity, "detail": detail})

    seen: set[str] = set()
    for kind in ("hard_constraints", "soft_constraints"):
        for atom in getattr(extraction, kind):
            key = canonical(atom.model_dump())
            if key in seen:
                report("duplicate_atom", atom.field, "Duplicate atom across constraint lists.")
            seen.add(key)
            if atom.field in LIST_ATOM_RULES:
                valid = (kind, atom.operator) in list_rules(atom.field)
            elif kind == "soft_constraints":
                valid = atom.operator == "prefer"
            elif atom.field == "destination":
                valid = atom.operator == "eq"
            elif atom.field != "other":
                valid = atom.operator in ("eq", "min", "max")
            else:
                valid = atom.operator != "prefer"
            if not valid:
                report(
                    "atom_classification_mismatch",
                    atom.field,
                    f"Unexpected {kind} operator {atom.operator!r} for this field.",
                )

    for field, operators in SCALAR_ATOM_OPERATORS.items():
        value = getattr(extraction, field)
        if value is None:
            continue
        values = {
            atom.value
            for atom in extraction.hard_constraints
            if atom.field == field and atom.operator in operators
        }
        if not values:
            report("missing_atom", field, "Populated scalar lacks a corresponding factual atom.")
        elif str(value) not in values:
            report("scalar_atom_mismatch", field, "Scalar differs from corresponding atom values.")
    for field in SCALAR_ATOM_OPERATORS:
        if getattr(extraction, field) is None and len(scalar_candidates(extraction, field)) == 1:
            report("missing_scalar", field, "A factual atom states a value the scalar omits.")

    for field in LIST_ATOM_RULES:
        values = set(getattr(extraction, field))
        atom_values = {
            atom.value
            for kind, operator in list_rules(field)
            for atom in getattr(extraction, kind)
            if atom.field == field and atom.operator == operator
        }
        if values - atom_values:
            report("missing_atom", field, "List entries lack corresponding atoms.")
        if atom_values - values:
            report(
                "list_atom_mismatch", field, "Atom values are absent from the corresponding list."
            )

    if extraction.missing_fields != derive_missing_fields(extraction):
        report(
            "missing_fields_mismatch", "missing_fields", "Missing fields differ from null logic."
        )

    # Only these conflicts have mechanically checkable evidence in the schema.
    conflicts = observed_conflicts(extraction)
    for atom in extraction.hard_constraints:
        if (
            atom.field == "budget_vnd"
            and atom.operator in ("min", "max")
            and not canonical_int(atom.value)
        ):
            report(
                "budget_bound_requires_review",
                "budget_vnd",
                "Budget bound cannot be checked as a canonical integer.",
                "review",
            )
    for conflict, present in conflicts.items():
        declared = conflict in extraction.possible_conflicts
        if present is not None and present != declared:
            report(
                "conflict_metadata_mismatch",
                "possible_conflicts",
                f"{conflict}: declared={declared}, mechanically observed={present}.",
            )
        elif present is None and declared:
            report(
                "conflict_metadata_unverifiable",
                "possible_conflicts",
                f"{conflict}: required explicit values are absent.",
                "review",
            )

    if "destination_disagreement" in extraction.possible_conflicts:
        destinations = {
            atom.value
            for atom in extraction.hard_constraints
            if atom.field == "destination" and atom.operator == "eq"
        }
        if extraction.destination is not None or len(destinations) < 2:
            report(
                "conflict_metadata_mismatch",
                "possible_conflicts",
                "Declared destination disagreement requires a null scalar and distinct eq atoms.",
            )

    start, end = extraction.available_start, extraction.available_end
    if start is not None and end is not None:
        if end < start:
            report(
                "overnight_relation_requires_review",
                "available_time",
                "End precedes start on the clock; the schema lacks a day offset, so the day "
                "relationship and time_order cannot be independently verified.",
                "review",
            )
        elif "time_order" in extraction.possible_conflicts:
            report(
                "conflict_metadata_mismatch",
                "possible_conflicts",
                "Declared time_order is not supported by the clock endpoints.",
            )
    elif "time_order" in extraction.possible_conflicts:
        report(
            "conflict_metadata_unverifiable",
            "possible_conflicts",
            "time_order: exact endpoints are absent; day relationship cannot be verified.",
            "review",
        )
    if extraction.possible_conflicts and not extraction.needs_clarification:
        report(
            "conflict_requires_clarification",
            "needs_clarification",
            "Declared conflicts require clarification; flags were not changed.",
        )
    return issues
