from typing import Any

from app.services.retrieval.hours import DAYS, TripWindow, to_minutes
from app.services.retrieval.interests import plan_interests
from app.services.retrieval.poi import Poi
from app.services.retrieval.sections import assemble, diversify, to_card
from tests.test_retrieval_names_interests import extraction

ANY = TripWindow(None, None, None)
EVENING = {day: [["18:00", "23:00"]] for day in DAYS}


def poi(poi_id: str, category: str = "food.restaurant", **fields: Any) -> Poi:
    kind = category.rsplit(".", 1)[-1]
    return Poi(id=poi_id, name=fields.pop("name", poi_id), kind=kind, category=category, **fields)


def reasons_of(results, poi_id: str) -> list[str]:
    return next(to_card(i).reasons for r in results for i in r.items if i.poi.id == poi_id)


def sections_for(pois: list[Poi], hits: dict | None = None, window: TripWindow = ANY, **ext: Any):
    plan = plan_interests(extraction(**ext))
    results, warnings = assemble(plan, {p.id: p for p in pois}, hits or {}, window)
    return {r.spec.key: [i.poi.id for i in r.items] for r in results}, results, warnings


def test_keyword_merges_into_host_section_and_lifts_hits() -> None:
    pois = [
        poi("fancy", tier="A", michelin="star", recommended_by_count=3),
        poi("pho-hung", name="Phở Hùng", tier="C"),
        poi("pagoda", "culture_religion.pagoda"),
    ]
    sections, results, warnings = sections_for(
        pois, {"kw:pho": {"pho-hung": "name"}}, interests=["food", "phở", "culture"]
    )
    assert sections == {"food": ["pho-hung", "fancy"], "culture": ["pagoda"]}
    food = next(r for r in results if r.spec.key == "food")
    assert [r.raw for r in food.spec.merged] == ["phở"]
    assert "Khớp “phở”" in to_card(food.items[0]).reasons
    assert warnings == []


def test_keyword_without_host_gets_its_own_section() -> None:
    sections, _, _ = sections_for(
        [poi("pho-hung"), poi("museum-1", "attraction.museum")],
        {"kw:pho": {"pho-hung": "name"}},
        interests=["museum", "phở"],
    )
    assert sections == {"museum": ["museum-1"], "kw:pho": ["pho-hung"]}


def test_poi_lands_in_one_section_the_more_specific_on_ties() -> None:
    # food.cafe (depth 2) beats food (depth 1); museum and culture both list attraction.museum,
    # and museum has fewer nodes.
    sections, _, _ = sections_for(
        [poi("m", "attraction.museum"), poi("c", "food.cafe"), poi("t", "attraction.landmark")],
        interests=["culture", "museum", "food", "cafe"],
    )
    assert sections == {"culture": ["t"], "museum": ["m"], "cafe": ["c"]}


def test_new_sub_under_a_listed_top_joins_its_interest() -> None:
    sections, _, _ = sections_for([poi("stall", "food.some_future_sub")], interests=["food"])
    assert sections == {"food": ["stall"]}


def test_weak_hit_outside_the_keyword_home_is_dropped() -> None:
    shops = [poi(f"shop{i}") for i in range(3)]
    market = poi("market", "shopping.market")
    hits = {"kw:pho": {**{s.id: "name" for s in shops}, "market": "text"}}
    sections, results, _ = sections_for([*shops, market], hits, interests=["shopping", "phở"])
    assert set(sections["kw:pho"]) == {s.id for s in shops}
    assert sections["shopping"] == ["market"]
    assert not any("phở" in reason for reason in reasons_of(results, "market"))


def test_weak_hit_inside_the_home_is_kept() -> None:
    shops = [poi("shop1"), poi("shop2"), poi("described")]
    hits = {"kw:pho": {"shop1": "name", "shop2": "name", "described": "text"}}
    sections, _, _ = sections_for(shops, hits, interests=["phở"])
    assert set(sections["kw:pho"]) == {"shop1", "shop2", "described"}


def test_scattered_keyword_gets_its_own_section_with_strong_hits_only() -> None:
    pois = [poi("a"), poi("b", "attraction.landmark"), poi("c"), poi("d", "shopping.market")]
    hits = {"kw:rooftop": {"a": "name", "b": "name", "d": "text"}}
    sections, results, _ = sections_for(pois, hits, interests=["food", "rooftop"])
    assert set(sections["kw:rooftop"]) == {"a", "b"}
    assert sections["food"] == ["c"]
    assert not next(r for r in results if r.spec.key == "food").spec.merged


def test_only_weak_scattered_hits_are_all_kept() -> None:
    pois = [poi("a"), poi("b", "attraction.landmark")]
    sections, _, _ = sections_for(pois, {"kw:view": {"a": "text", "b": "text"}}, interests=["view"])
    assert set(sections["kw:view"]) == {"a", "b"}


def test_home_falls_back_to_top_level_and_needs_a_covering_host() -> None:
    pois = [poi("r1"), poi("r2"), poi("c1", "food.cafe"), poi("c2", "food.cafe")]
    hits = {"kw:tapas": {p.id: "structured" for p in pois}}
    # Home is `food` (no sub holds 60%): food.cafe does not cover it, food does.
    assert "kw:tapas" in sections_for(pois, hits, interests=["cafe", "tapas"])[0]
    sections, results, _ = sections_for(pois, hits, interests=["food", "tapas"])
    assert set(sections) == {"food"}
    assert [r.raw for r in results[0].spec.merged] == ["tapas"]


def test_sections_follow_the_travellers_order() -> None:
    pois = [poi("c", "food.cafe"), poi("pho"), poi("t", "culture_religion.temple")]
    _, results, _ = sections_for(
        pois, {"kw:pho": {"pho": "name"}}, interests=["cafe", "phở", "culture"]
    )
    assert [r.spec.key for r in results] == ["cafe", "kw:pho", "culture"]


def test_bars_only_in_nightlife() -> None:
    pois = [poi("go2", "entertainment.nightlife"), poi("coffee", "food.cafe")]
    assert sections_for(pois, interests=["cafe"])[0] == {"cafe": ["coffee"]}
    assert sections_for(pois, interests=["cafe", "bar"])[0] == {
        "cafe": ["coffee"],
        "nightlife": ["go2"],
    }


def test_trusted_closed_is_excluded_doubtful_closed_is_flagged() -> None:
    morning = TripWindow(None, to_minutes("07:00"), to_minutes("09:00"))
    pois = [
        poi("trusted", opening_hours=EVENING, hours_confidence="high", hours_contradicted=False),
        poi("doubtful", opening_hours=EVENING, hours_confidence="low", hours_contradicted=False),
        poi("unknown"),
    ]
    sections, results, _ = sections_for(pois, window=morning, interests=["food"])
    assert set(sections["food"]) == {"doubtful", "unknown"}
    flags = {i.poi.id: i.flags for i in results[0].items}
    assert flags["doubtful"] == ["hours_maybe_closed"]
    assert flags["unknown"] == ["hours_unknown"]


def test_avoid_category_and_filter() -> None:
    pois = [poi("star", michelin="star"), poi("plain"), poi("m", "attraction.museum")]
    sections, _, _ = sections_for(pois, interests=["food", "culture"], avoid=["michelin", "bảo tàng"])
    assert sections == {"food": ["plain"]}


def test_required_empty_and_beach_warn() -> None:
    sections, results, warnings = sections_for(
        [poi("r")],
        interests=["beach", "park"],
        hard_constraints=[{"field": "interests", "operator": "include", "value": "beach"}],
    )
    assert sections == {"beach": []}
    assert results[0].spec.required and results[0].spec.note
    assert any("beach" in w for w in warnings)
    assert any("park" in w for w in warnings)


def test_no_interest_uses_default_sections() -> None:
    sections, _, _ = sections_for(
        [poi("low", tier="C"), poi("high", tier="A"), poi("pagoda", "culture_religion.pagoda")]
    )
    assert set(sections["food"]) == {"high", "low"}
    assert sections["culture"] == ["pagoda"]


def ranked(pois: list[Poi], seed: int, hits: dict | None = None, **ext: Any) -> list[str]:
    plan = plan_interests(extraction(**ext))
    results, _ = assemble(plan, {p.id: p for p in pois}, hits or {}, ANY, seed=seed)
    return [i.poi.id for r in results for i in r.items]


# Equally relevant restaurants: 10 well-documented, 40 with no recommendations.
FAMOUS = [poi(f"a{i}", tier="A", recommended_by_count=3, confidence=0.9) for i in range(10)]
PLAIN = [poi(f"d{i}", tier="D", confidence=0.9) for i in range(40)]


def test_equal_relevance_order_is_seeded_not_by_quality() -> None:
    orders = {seed: ranked([*FAMOUS, *PLAIN], seed, interests=["food"]) for seed in range(20)}
    assert all(ranked([*FAMOUS, *PLAIN], s, interests=["food"]) == orders[s] for s in (0, 7))
    assert len({tuple(order[:6]) for order in orders.values()}) == 20
    firsts = {order[0][0] for order in orders.values()}
    assert firsts == {"a", "d"}  # quality alone does not decide the top


def test_famous_places_are_capped_per_window() -> None:
    for seed in range(20):
        top = ranked([*FAMOUS, *PLAIN], seed, interests=["food"])[:8]
        assert sum(poi_id.startswith("a") for poi_id in top) <= 3


def test_michelin_request_is_not_capped() -> None:
    stars = [poi(f"s{i}", tier="A", michelin="star", confidence=0.9) for i in range(10)]
    hits = {"kw:michelin": {p.id: "structured" for p in stars}}
    for seed in range(5):
        assert len(ranked([*stars, *PLAIN], seed, hits, interests=["michelin"])[:8]) == 8


def test_real_signals_beat_tier_for_every_seed() -> None:
    famous_text = poi("famous", tier="A", recommended_by_count=4, michelin="star", confidence=1.0)
    plain_name = poi("plain", tier="D", confidence=0.8)
    hits = {"kw:pho": {"famous": "text", "plain": "name"}}
    evening = TripWindow(None, to_minutes("19:00"), to_minutes("21:00"))
    morning_only = {day: [["06:00", "11:00"]] for day in DAYS}
    doubtful = poi(
        "doubtful",
        tier="A",
        recommended_by_count=4,
        confidence=1.0,
        opening_hours=morning_only,
        hours_confidence="low",
    )
    open_plain = poi("open", tier="D", confidence=0.8, opening_hours=EVENING)
    plan = plan_interests(extraction(interests=["food"]))
    for seed in range(20):
        assert ranked([famous_text, plain_name], seed, hits, interests=["phở"])[0] == "plain"
        results, _ = assemble(
            plan, {"doubtful": doubtful, "open": open_plain}, {}, evening, seed=seed
        )
        assert results[0].items[0].poi.id == "open"


def test_preferred_first_and_orphans_get_their_own_section() -> None:
    plan = plan_interests(extraction(interests=["food"]))
    pois = {p.id: p for p in [poi("a", tier="A"), poi("b", tier="C"), poi("m", "attraction.museum")]}
    results, _ = assemble(plan, pois, {}, ANY, preferred_ids=frozenset({"b", "m"}))
    assert [(r.spec.key, [i.poi.id for i in r.items]) for r in results] == [
        ("food", ["b", "a"]),
        ("preferred", ["m"]),
    ]


def test_diversity_caps_repeated_cuisine_in_window() -> None:
    plan = plan_interests(extraction(interests=["food"]))
    pois = {
        p.id: p
        for p in [
            poi("ramen1", cuisine=("ramen",), tier="A"),
            poi("ramen2", cuisine=("ramen",), tier="A"),
            poi("ramen3", cuisine=("ramen",), tier="A"),
            poi("thai", cuisine=("thai",), tier="C"),
        ]
    }
    results, _ = assemble(plan, pois, {}, ANY)
    ids = [i.poi.id for i in results[0].items]
    assert len(ids) == 4 and "thai" in ids[:3]  # at most two ramen before it, whatever the seed
    assert diversify([], famous_capped=True) == []


def test_reasons_come_from_columns() -> None:
    _, results, _ = sections_for(
        [
            poi(
                "x",
                michelin="bib_gourmand",
                cuisine=("vietnamese",),
                recommended_by_count=2,
                price_raw="30,000 dong",
                opening_hours={day: [["06:00", "22:00"]] for day in DAYS},
            )
        ],
        interests=["food"],
    )
    card = to_card(results[0].items[0])
    assert card.reasons == [
        "Michelin Bib Gourmand",
        "Ẩm thực Việt",
        "Mở 06:00–22:00 hằng ngày",
        "Được 2 nguồn giới thiệu",
        "Giá: 30,000 dong",
    ]
    assert "no_price" not in card.flags
    assert set(card.score_parts) == {"match", "quality", "explore"}


def test_skipped_place_is_not_listed_and_its_neighbours_rank_first() -> None:
    # p3 was already picked: it anchors proximity but must not be suggested again.
    cafes = {f"p{i}": poi(f"p{i}", "food.cafe", latitude=10.77 + i * 0.009, longitude=106.7)
             for i in range(5)}
    plan = plan_interests(extraction(interests=["cafe"]))
    results, _ = assemble(
        plan, cafes, {}, ANY, pinned_ids=frozenset({"p3"}), anchors=[(10.797, 106.7)], seed=1
    )
    ids = [i.poi.id for i in results[0].items]
    assert "p3" not in ids
    assert set(ids[:2]) == {"p2", "p4"}
