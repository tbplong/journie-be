"""Versioned retrieval config: interest groups, keyword map, per-POI overrides.

Loaded once at import time and validated, so a bad edit fails at startup, not mid-request.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.services.retrieval.taxonomy import SEPARATOR, covers, depth
from app.services.retrieval.text import fold_case, norm

CONFIG_DIR = Path(__file__).with_name("config")
# The extraction's alias table (kind,canonical,alias): the standard interests' own words.
NAME_ALIASES_CSV = Path(__file__).parents[1] / "extraction" / "name_aliases.csv"
FILTER_COLUMNS = ("michelin", "cuisine", "features")
# `category` fixes data errors (bars tagged food.cafe); `kind` is display only.
OVERRIDE_FIELDS = {"category", "kind"}
MAX_NODE_DEPTH = 2  # taxonomy is top.sub


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    nodes: tuple[str, ...]  # taxonomy nodes, see taxonomy.py

    def node_for(self, category: str | None) -> str | None:
        """The deepest of this group's nodes covering ``category``, if any."""
        return max(
            (node for node in self.nodes if covers(node, category)), key=depth, default=None
        )


@dataclass(frozen=True)
class Keyword:
    key: str
    label: str
    aliases: tuple[str, ...]  # norm() forms, for the exact lookup of a whole value
    # Column -> accepted values; a POI matches if any listed column has any value.
    filter: dict[str, tuple[str, ...]] = field(default_factory=dict)
    group: str | None = None
    # Also run the text search and merge its hits (for words that appear in names). On a group
    # keyword: the group plus a text request for the traveller's words.
    text: bool = False
    # The aliases as written (fold_case, accents kept): a group alias is also looked for inside
    # a longer wish, under the accent rule (interests.contained_group).
    written: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterestMap:
    version: str
    taxonomy: str
    groups: dict[str, Group]
    defaults: tuple[str, ...]
    gaps: dict[str, str]  # nodes used although missing from the taxonomy -> why


@dataclass(frozen=True)
class KeywordMap:
    version: str
    by_alias: dict[str, Keyword]
    cuisine_labels: dict[str, str]
    # Times, not wishes ("tối nay"), and phrases that hold a group alias without meaning it
    # ("bia tiến sĩ"); both as written (fold_case).
    ignored_wishes: tuple[str, ...] = ()
    not_aliases: tuple[str, ...] = ()
    # Alias words matched inside an unaccented wish only as written ("đền" is not "den" = đến).
    accent_only: tuple[str, ...] = ()
    # Words that add nothing to an alias of a group without data ("bãi biển", "đi tắm biển").
    filler_words: tuple[str, ...] = ()


def _yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def load_interest_map(name: str = "interest_map.v2.yaml") -> InterestMap:
    """Shape checks only; tests check the nodes against the taxonomy file, which the deployed
    app does not ship (data/ is not bundled)."""
    data = _yaml(name)
    groups = {
        key: Group(key, spec["label"], tuple(spec["nodes"])) for key, spec in data["groups"].items()
    }
    for group in groups.values():
        for node in group.nodes:
            parts = node.split(SEPARATOR)
            if not all(parts) or len(parts) > MAX_NODE_DEPTH:
                raise ValueError(f"Group {group.key!r} has a malformed node {node!r}")
    for key in data["defaults"]:
        if key not in groups:
            raise ValueError(f"Default group {key!r} is not defined")
    return InterestMap(
        data["version"],
        data["taxonomy"],
        groups,
        tuple(data["defaults"]),
        dict(data.get("taxonomy_gaps") or {}),
    )


def load_keyword_map(groups: dict[str, Group], name: str = "keyword_map.v1.yaml") -> KeywordMap:
    data = _yaml(name)
    by_alias: dict[str, Keyword] = {}
    for key, spec in data["keywords"].items():
        column_filter = {col: tuple(vals) for col, vals in (spec.get("filter") or {}).items()}
        if set(column_filter) - set(FILTER_COLUMNS):
            raise ValueError(f"Keyword {key!r} filters an unknown column")
        if bool(column_filter) == bool(spec.get("group")):
            raise ValueError(f"Keyword {key!r} needs exactly one of filter/group")
        if spec.get("group") and spec["group"] not in groups:
            raise ValueError(f"Keyword {key!r} routes to unknown group {spec['group']!r}")
        keyword = Keyword(
            key,
            spec["label"],
            tuple(norm(alias) for alias in spec["aliases"]),
            column_filter,
            spec.get("group"),
            spec.get("text", False),
            tuple(fold_case(alias) for alias in spec["aliases"]),
        )
        if not all(keyword.aliases):
            raise ValueError(f"Keyword {key!r} has an empty alias")
        for alias in keyword.aliases:
            if by_alias.setdefault(alias, keyword) is not keyword:
                raise ValueError(f"Alias {alias!r} maps to two keywords")
    ignored = tuple(fold_case(value) for value in data.get("ignored_wishes") or ())
    not_aliases = tuple(fold_case(value) for value in data.get("not_aliases") or ())
    filler = tuple(fold_case(value) for value in data.get("filler_words") or ())
    accent_only = tuple(fold_case(value) for value in data.get("accent_only") or ())
    if not all(ignored) or not all(not_aliases):
        raise ValueError("ignored_wishes and not_aliases need words")
    if not all(filler) or any(len(word.split()) != 1 for word in filler):
        raise ValueError("filler_words are single words")
    if clash := {norm(value) for value in ignored} & set(by_alias):
        raise ValueError(f"Ignored wishes {sorted(clash)} are also aliases")
    return KeywordMap(
        data["version"], by_alias, data.get("cuisine_labels", {}), ignored, not_aliases, accent_only,
        filler,
    )


def load_interest_aliases(
    groups: dict[str, Group], path: Path = NAME_ALIASES_CSV
) -> dict[str, str]:
    """Standard-interest names and aliases as written (fold_case) -> group key, so a wish
    holding one ("cà phê yên tĩnh") can be routed to its group."""
    aliases: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["kind"] == "interest" and row["canonical"] in groups:
                for name in (row["canonical"], row["alias"]):
                    aliases[fold_case(name)] = row["canonical"]
    return aliases


def load_overrides(name: str = "poi_overrides.csv") -> dict[str, dict[str, str]]:
    """id -> field -> raw value; applied by the importer after the automatic rules."""
    overrides: dict[str, dict[str, str]] = {}
    with (CONFIG_DIR / name).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["field"] not in OVERRIDE_FIELDS:
                raise ValueError(f"Override field {row['field']!r} is not supported")
            overrides.setdefault(row["id"], {})[row["field"]] = row["value"]
    return overrides


INTEREST_MAP = load_interest_map()
KEYWORD_MAP = load_keyword_map(INTEREST_MAP.groups)
INTEREST_ALIASES = load_interest_aliases(INTEREST_MAP.groups)
