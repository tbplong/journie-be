"""Visit length per place (app/services/routing/visits.py, config/visit_minutes.yaml)."""

from dataclasses import replace
from pathlib import Path

import pytest

from app.services.retrieval.poi import Poi
from app.services.retrieval.sections import Scored, to_card
from app.services.routing.visits import VISIT_MINUTES, load_visit_minutes
from tests.test_retrieval_taxonomy import TAXONOMY
from tests.test_routing_api import plan, poi, request


def place(name: str, category: str = "food.restaurant", **extra) -> Poi:
    return Poi(id=extra.pop("id", "x"), name=name, kind="x", category=category, **extra)


@pytest.mark.parametrize(
    ("name", "extra", "minutes"),
    [
        ("Phở Hòa Pasteur", {}, 35),
        ("Pho Hoa Pasteur", {}, 60),  # without marks: no style, the category value
        ("Xin Chao Restaurant", {}, 60),  # not cháo
        ("Mỳ Quảng Bà Mua", {}, 35),
        ("Nem Nướng Oanh Vy", {}, 35),  # quick, though nướng is in the long list
        ("Gogi House", {"cuisine": ("hotpot",)}, 75),
        ("Ramen Danbo", {"cuisine": ("ramen",)}, 35),
        ("Bún Thịt Nướng Kiều Bảo", {}, 35),  # quick comes before long
        ("Huê Ký Mì Gia", {}, 35),
        ("Lẩu Bò Subin", {}, 75),
        ("Sukdon BBQ", {}, 75),
        ("The Gangs", {"cuisine": ("barbecue",)}, 75),
        ("Nhà hàng Lầu 3", {}, 60),  # lầu is a floor, not lẩu
        ("Phởlicious", {}, 60),  # whole words only
        ("Ănăn Saigon", {"michelin": "star"}, 90),
        ("Cục Gạch Quán", {}, 60),
    ],
)
def test_restaurant_style(name: str, extra: dict, minutes: int) -> None:
    assert VISIT_MINUTES.of_place(place(name, **extra)) == minutes


def test_the_place_value_comes_first_then_the_category_then_the_default() -> None:
    assert VISIT_MINUTES.of_place(place("x", "nature.garden", id="thao-cam-vien-sai-gon-zoo-botanical-gard-034c49")) == 90  # fmt: skip
    assert VISIT_MINUTES.of_place(place("Chợ Bún", "shopping.market")) == 60  # style is food only
    assert VISIT_MINUTES.of_place(place("Chùa", "culture_religion.pagoda")) == 40
    assert VISIT_MINUTES.of_place(place("?", "made.up")) == VISIT_MINUTES.default


def test_a_sight_takes_at_most_90_minutes_and_anything_else_120() -> None:
    assert all(v <= VISIT_MINUTES.cap(c) for c, v in VISIT_MINUTES.by_category.items())
    assert max(VISIT_MINUTES.by_place.values()) <= 120
    assert VISIT_MINUTES.cap("attraction.zoo") == VISIT_MINUTES.cap("shopping.market") == 90
    assert VISIT_MINUTES.cap("entertainment.cinema") == VISIT_MINUTES.cap("food.restaurant") == 120


def test_the_cap_applies_to_a_place_value_too(tmp_path: Path) -> None:
    path = tmp_path / "visit_minutes.yaml"
    path.write_text(
        "version: t\ndefault: 40\ncategories: {attraction.landmark: 45}\n"
        "max_minutes: {default: 120, attraction: 90}\nplaces: {big: 120}\n"
    )
    table = load_visit_minutes(path)
    assert table.of_place(place("Cầu Vàng", "attraction.landmark", id="big")) == 90
    assert table.of_place(place("Jazz", "entertainment.performing_arts", id="big")) == 120


def test_categories_are_taxonomy_values() -> None:
    assert set(VISIT_MINUTES.by_category) <= TAXONOMY.categories


@pytest.mark.parametrize(
    "bad",
    [
        "places: {a: 0}",
        "places: {a: 45.5}",
        "places: {a: true}",
        "restaurant_style: {quick: {minutes: 35, words: []}}",
        "restaurant_style: {quick: {minutes: -5, words: [phở]}}",
        "restaurant_style: {michelin_star: 0}",
        "max_minutes: 120\nplaces: {a: 150}",
        "max_minutes: {default: 120, food: 40}",
    ],
)
def test_bad_values_fail_at_load(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "visit_minutes.yaml"
    path.write_text(f"version: t\ndefault: 40\ncategories: {{food.cafe: 45}}\n{bad}\n")
    with pytest.raises(ValueError):
        load_visit_minutes(path)


def test_the_card_carries_the_visit_length() -> None:
    zoo = place("Thảo Cầm Viên", "nature.garden", id="thao-cam-vien-sai-gon-zoo-botanical-gard-034c49")  # fmt: skip
    card = to_card(Scored(zoo, "pinned", [], None, [], {}, 1.0, False))  # type: ignore[arg-type]
    assert card.visit_minutes == 90


def test_routing_times_a_stop_with_the_place_value(monkeypatch: pytest.MonkeyPatch) -> None:
    pho = replace(poi("pho", 0.001), name="Phở Lệ")
    result = plan(monkeypatch, [pho], request(["pho"], "2026-11-20T09:00", "2026-11-20T18:00"))
    [stop] = result.days[0].stops
    assert stop.visit_minutes == 35
