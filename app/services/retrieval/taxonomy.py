"""Category taxonomy (journie-data ``taxonomy.v1.yaml``): the source of truth for sections.

A POI's ``category`` is one ``top.sub`` value. A *node* is either a top-level category
(``food``: every ``food.*`` sub) or one sub (``food.cafe``). Interests are lists of nodes, so a
sub added under a listed top joins its interest with no backend change. Pure, except
``load_taxonomy`` which reads the file (importer and tests only; the app trusts imported data).
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

SEPARATOR = "."


def top(node: str) -> str:
    return node.split(SEPARATOR, 1)[0]


def depth(node: str) -> int:
    """1 for a top-level node, 2 for a sub; deeper is more specific."""
    return node.count(SEPARATOR) + 1


def covers(node: str, category: str | None) -> bool:
    return bool(category) and (category == node or category.startswith(node + SEPARATOR))


@dataclass(frozen=True)
class Taxonomy:
    version: str
    categories: frozenset[str]  # valid POI values: every top.sub

    @property
    def nodes(self) -> frozenset[str]:
        return self.categories | {top(category) for category in self.categories}


def load_taxonomy(path: Path) -> Taxonomy:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    categories = frozenset(
        f"{name}{SEPARATOR}{sub}" for name, subs in data["categories"].items() for sub in subs
    )
    return Taxonomy(data["version"], categories)
