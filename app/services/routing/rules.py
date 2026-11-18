"""Day-level travel rules from config/rules.yaml. Pure; loaded at import so a bad edit fails at
startup. The no-two-food-in-a-row rule is enforced by the solver through Instance.no_adjacent."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from app.services.retrieval.taxonomy import covers

RULES_FILE = Path(__file__).with_name("config") / "rules.yaml"


@dataclass(frozen=True)
class Rules:
    version: str
    day_max: dict[str, int]
    day_spread: tuple[str, ...]
    not_adjacent: tuple[str, ...]
    balance_tolerance: float

    def tag(self, category: str | None) -> str:
        """The not_adjacent node covering this category, or "" when none does."""
        return next((node for node in self.not_adjacent if covers(node, category)), "")

    def broken(self, days: list[list[str | None]], dates: list[date]) -> tuple[int, list[str]]:
        """Excess over the day rules (0 = all kept) and a warning per broken rule, for the
        categories visited on each day."""
        excess, warnings = 0, []
        for node, limit in self.day_max.items():
            for day, categories in zip(dates, days, strict=True):
                over = sum(covers(node, c) for c in categories) - limit
                if over > 0:
                    excess += over
                    warnings.append(f"day_max:{node}:{day}")
        for node in self.day_spread:
            counts = [sum(covers(node, c) for c in categories) for categories in days]
            over = max(counts, default=0) - min(counts, default=0) - 1
            if over > 0:
                excess += over
                warnings.append(f"day_spread:{node}")
        return excess, warnings


def load_rules(path: Path = RULES_FILE) -> Rules:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = Rules(
        data["version"],
        {node: int(limit) for node, limit in data["day_max"].items()},
        tuple(data["day_spread"]),
        tuple(data["not_adjacent"]),
        float(data["balance_tolerance"]),
    )
    if any(limit < 0 for limit in rules.day_max.values()) or not 0 <= rules.balance_tolerance < 1:
        raise ValueError(f"{path.name}: limits must be >= 0 and balance_tolerance in [0, 1)")
    return rules


RULES = load_rules()
