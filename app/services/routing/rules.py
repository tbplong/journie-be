"""Day-level travel rules from config/rules.yaml. Pure; loaded at import so a bad edit fails at
startup. The no-two-food-in-a-row rule is enforced by the solver through Instance.no_adjacent."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.services.retrieval.hours import to_minutes
from app.services.retrieval.taxonomy import covers, depth

RULES_FILE = Path(__file__).with_name("config") / "rules.yaml"
# Positions in meal_windows; load_rules checks there are three when a rule reads them.
LUNCH, DINNER = 1, 2


@dataclass(frozen=True)
class Rules:
    version: str
    day_max: dict[str, int]
    day_spread: tuple[str, ...]
    not_adjacent: tuple[str, ...]
    balance_tolerance: float
    not_before: dict[str, int] = field(default_factory=dict)  # node -> seconds after midnight
    meal_nodes: tuple[str, ...] = ()
    meal_windows: tuple[tuple[int, int], ...] = ()  # (from, until) seconds after midnight
    meal_penalty: int = 0  # seconds of travel one meal outside those hours counts as
    meal_one_per_window: bool = False  # a second meal in a window used that day is off-hours
    slot_nodes: tuple[str, ...] = ()  # meals pinned to a day that take one meal window each
    slot_order: tuple[int, ...] = ()  # positions in meal_windows, in the order meals take them
    free_from: int | None = None  # a visit starting at or after this begins the evening
    free_allow: int = 0  # seconds of waiting before an evening visit that are free time
    after_penalty: int = 0  # seconds a meal or daytime visit after the evening began counts as
    avoid_nodes: tuple[str, ...] = ()  # outdoor sights that should not start in avoid_windows
    avoid_windows: tuple[tuple[int, int], ...] = ()  # (from, until) seconds after midnight
    avoid_penalty: int = 0  # seconds one such start counts as
    rest_from: int = 12 * 3600  # the afternoon begins here on a day without a lunch
    rest_max: int = 0  # seconds of free time moved from before the evening to after lunch
    late_start: bool = False  # the morning starts later when its lunch would wait
    rest_by_stops: tuple[tuple[int, int], ...] = ()  # (stops up to, seconds of rest), ascending

    def meal_window(self, at: int) -> int | None:
        """Position in meal_windows of the window this time of day falls in, or None."""
        return next(
            (k for k, (start, end) in enumerate(self.meal_windows) if start <= at < end), None
        )

    def has_slot(self, category: str | None) -> bool:
        return any(covers(node, category) for node in self.slot_nodes)

    def is_avoided(self, category: str | None) -> bool:
        return any(covers(node, category) for node in self.avoid_nodes)

    def avoid_at(self, at: int) -> bool:
        """A visit starting at this time of day (seconds after midnight) is inside avoid hours."""
        return any(start <= at < end for start, end in self.avoid_windows)

    def tag(self, category: str | None) -> str:
        """The not_adjacent node covering this category, or "" when none does."""
        return next((node for node in self.not_adjacent if covers(node, category)), "")

    def earliest(self, category: str | None) -> tuple[str, int] | None:
        """(node, seconds after midnight) of the not_before rule covering this category: the
        most specific node wins."""
        nodes = [node for node in self.not_before if covers(node, category)]
        if not nodes:
            return None
        node = max(nodes, key=depth)
        return node, self.not_before[node]

    def is_meal(self, category: str | None) -> bool:
        return any(covers(node, category) for node in self.meal_nodes)

    def off_meal_hours(self, at: int) -> bool:
        """A meal starting at this time of day (seconds after midnight) is outside meal hours."""
        return not any(start <= at < end for start, end in self.meal_windows)

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


def _spans(path: Path, values: list[str]) -> tuple[tuple[int, int], ...]:
    """Spans written as "HH:MM-HH:MM", as (from, until) seconds after midnight; each must run
    forward."""
    spans = tuple(
        (to_minutes(a) * 60, to_minutes(b) * 60) for a, b in (v.split("-") for v in values)
    )
    if any(start >= end for start, end in spans):
        raise ValueError(f"{path.name}: hours must run forward")
    return spans


def load_rules(path: Path = RULES_FILE) -> Rules:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    meals = data.get("meal_hours") or {}
    slots = meals.get("slots") or {}
    evening = data.get("evening") or {}
    avoid = data.get("avoid_hours") or {}
    rest = data.get("midday_rest") or {}
    meal_windows = _spans(path, meals.get("windows", []))
    rules = Rules(
        data["version"],
        {node: int(limit) for node, limit in data["day_max"].items()},
        tuple(data["day_spread"]),
        tuple(data["not_adjacent"]),
        float(data["balance_tolerance"]),
        {node: to_minutes(at) * 60 for node, at in (data.get("not_before") or {}).items()},
        tuple(meals.get("nodes", [])),
        meal_windows,
        int(meals.get("penalty_minutes", 0)) * 60,
        meal_one_per_window=bool(meals.get("one_per_window", False)),
        slot_nodes=tuple(slots.get("nodes", [])),
        slot_order=tuple(int(k) for k in slots.get("order", [])),
        free_from=to_minutes(evening["free_from"]) * 60 if evening.get("free_from") else None,
        free_allow=int(evening.get("free_minutes", 0)) * 60,
        after_penalty=int(evening.get("after_penalty_minutes", 0)) * 60,
        avoid_nodes=tuple(avoid.get("nodes", [])),
        avoid_windows=_spans(path, avoid.get("windows", [])),
        avoid_penalty=int(avoid.get("penalty_minutes", 0)) * 60,
        rest_from=to_minutes(rest.get("from", "12:00")) * 60,
        rest_max=int(rest.get("max_minutes", 0)) * 60,
        late_start=bool(rest.get("late_start", False)),
        rest_by_stops=tuple((int(n), int(m) * 60) for n, m in rest.get("minutes_by_stops", [])),
    )
    seconds = [rules.meal_penalty, rules.free_allow, rules.after_penalty, rules.avoid_penalty]
    seconds += [rules.rest_max, *(length for _, length in rules.rest_by_stops)]
    if any(value < 0 for value in seconds):
        raise ValueError(f"{path.name}: penalties and minutes must be >= 0")
    stops = [n for n, _ in rules.rest_by_stops]
    if stops != sorted(set(stops)):
        raise ValueError(f"{path.name}: midday_rest.minutes_by_stops must ascend by stops")
    order = rules.slot_order
    if len(set(order)) != len(order) or any(not 0 <= k < len(meal_windows) for k in order):
        raise ValueError(f"{path.name}: meal_hours.slots.order must hold distinct window positions")
    if (slots or evening or rest) and len(meal_windows) != 3:
        raise ValueError(
            f"{path.name}: slots, evening and midday_rest need breakfast, lunch and dinner windows"
        )
    if any(limit < 0 for limit in rules.day_max.values()) or not 0 <= rules.balance_tolerance < 1:
        raise ValueError(f"{path.name}: limits must be >= 0 and balance_tolerance in [0, 1)")
    return rules


RULES = load_rules()
