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

    def counts(self, categories: list[str | None]) -> tuple[int, ...]:
        """One day's places under each day_max node, then under each day_spread node."""
        nodes = (*self.day_max, *self.day_spread)
        return tuple(sum(covers(node, c) for c in categories) for node in nodes)

    def overs(self, days: list[tuple[int, ...]]) -> list[tuple[int, str, int | None]]:
        """(excess, node, day index or None for a spread rule) of every broken rule, from the
        ``counts`` of each day. The planner's cost calls this with cached counts."""
        found = []
        for k, (node, limit) in enumerate(self.day_max.items()):
            for d, counts in enumerate(days):
                if counts[k] > limit:
                    found.append((counts[k] - limit, node, d))
        for k, node in enumerate(self.day_spread, start=len(self.day_max)):
            column = [counts[k] for counts in days]
            over = max(column, default=0) - min(column, default=0) - 1
            if over > 0:
                found.append((over, node, None))
        return found

    def broken(self, days: list[list[str | None]], dates: list[date]) -> tuple[int, list[str]]:
        """Excess over the day rules (0 = all kept) and a warning per broken rule, for the
        categories visited on each day."""
        overs = self.overs([self.counts(categories) for categories in days])
        warnings = [
            f"day_max:{node}:{dates[d]}" if d is not None else f"day_spread:{node}"
            for _, node, d in overs
        ]
        return sum(over for over, _, _ in overs), warnings


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
