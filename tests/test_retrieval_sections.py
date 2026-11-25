from typing import Any

from app.services.retrieval.hours import DAYS, TripWindow, to_minutes
from app.services.retrieval.interests import plan_interests
from app.services.retrieval.poi import Poi
from app.services.retrieval.sections import NO_MATCH_DEFAULTS, assemble, diversify, to_card
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


def test_required_broad_interest_does_not_swallow_a_preferred_narrow_one() -> None:
    sections, _, warnings = sections_for(
        [poi("c", "food.cafe"), poi("r")],
        interests=["food", "cafe"],
        hard_constraints=[{"field": "interests", "operator": "include", "value": "food"}],
    )
    assert sections == {"food": ["r"], "cafe": ["c"]}
    assert not warnings


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
    pois = [
        poi("a"),
        poi("b", "attraction.landmark"),
        poi("e", "culture_religion.pagoda"),
        poi("c"),
        poi("d", "shopping.market"),
    ]
    hits = {"kw:rooftop": {"a": "name", "b": "name", "e": "name", "d": "text"}}
    sections, results, _ = sections_for(pois, hits, interests=["food", "rooftop"])
    assert set(sections["kw:rooftop"]) == {"a", "b", "e"}
    assert sections["food"] == ["c"]
    assert not next(r for r in results if r.spec.key == "food").spec.merged


def test_one_stray_name_hit_does_not_discard_the_description_hits() -> None:
    # Like "rượu" in HCM: one restaurant has the word in its name (Hầm Rượu); 8 bars, 3 other
    # restaurants and a cafe have it in their description. No node holds 60%.
    described = [
        *(poi(f"bar{i}", "entertainment.nightlife") for i in range(8)),
        *(poi(f"r{i}") for i in range(3)),
        poi("cafe", "food.cafe"),
    ]
    pois = [poi("named"), *described]
    hits = {"kw:den_long": {"named": "name", **{p.id: "text" for p in described}}}
    sections, _, _ = sections_for(pois, hits, interests=["đèn lồng"])
    assert set(sections["kw:den_long"]) == {p.id for p in pois}


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


def test_other_facts_and_fruitless_other_atoms_keep_the_default_sections() -> None:
    places = [poi("r"), poi("pagoda", "culture_religion.pagoda")]
    fact = {"field": "other", "operator": "eq", "value": "budget exactly 75 NZD"}
    assert plan_interests(extraction(hard_constraints=[fact])).requests == []
    assert sections_for(places, hard_constraints=[fact])[2] == []
    wish = {"field": "other", "operator": "prefer", "value": "đi cùng trẻ em"}
    sections, _, warnings = sections_for(places, soft_constraints=[wish])
    assert sections == {"food": ["r"], "culture": ["pagoda"]}
    # The defaults do not pretend to answer the wish.
    assert warnings == [NO_MATCH_DEFAULTS.format("đi cùng trẻ em")]
    assert warnings == ["Chưa tìm thấy địa điểm khớp “đi cùng trẻ em”; đang gợi ý chung"]
    # A refusal is not a wish for a place: no such note.
    refusal = {"field": "other", "operator": "prefer", "value": "không uống rượu"}
    assert sections_for(places, soft_constraints=[refusal])[2] == []
    # An `other` atom that finds places replaces the defaults, as an interest does.
    found = {"kw:di_cung_tre_em": {"r": "text"}}
    sections, _, warnings = sections_for(places, found, soft_constraints=[wish])
    assert sections == {"kw:di_cung_tre_em": ["r"]} and warnings == []


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


def test_preferred_orphan_before_other_places_does_not_crash() -> None:
    plan = plan_interests(extraction(interests=["food"]))
    pois = {p.id: p for p in [poi("m", "attraction.museum"), poi("a", tier="A"), poi("b")]}
    results, _ = assemble(plan, pois, {}, ANY, preferred_ids=frozenset({"m"}))
    assert [(r.spec.key, {i.poi.id for i in r.items}) for r in results] == [
        ("food", {"a", "b"}),
        ("preferred", {"m"}),
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


def test_semantic_hits_keep_the_closest_usable_places(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.services.retrieval.sections.SEMANTIC_KEEP", 2)
    plan = plan_interests(extraction(interests=["quán yên tĩnh để làm việc"]))
    key = plan.requests[0].key
    pois = {p.id: p for p in [poi("near"), poi("picked"), poi("mid"), poi("far"), poi("word")]}
    hits = {key: {"near": "semantic", "picked": "semantic", "mid": "semantic", "far": "semantic",
                  "word": "text"}}  # fmt: skip
    close = {key: {"near": 0.9, "picked": 0.8, "mid": 0.5, "far": 0.1}}
    results, _ = assemble(plan, pois, hits, ANY, pinned_ids=frozenset({"picked"}), closeness=close)
    # "picked" no longer takes a slot; the word hit is not a semantic hit and is always kept.
    assert {i.poi.id for r in results for i in r.items} == {"near", "mid", "word"}


def _semantic_only(
    key: str, pois: dict[str, Poi], order: list[str]
) -> tuple[dict, dict[str, dict[str, float]]]:
    """Semantic hits on ``order`` (closest first) for one request key."""
    close = {poi_id: 1 - n / len(order) for n, poi_id in enumerate(order)}
    return {key: dict.fromkeys(order, "semantic")}, {key: close}


def test_picking_places_refills_a_semantic_section_from_its_home(monkeypatch: Any) -> None:
    """N14: the SEMANTIC_KEEP closest places of the home, before and after picks."""
    monkeypatch.setattr("app.services.retrieval.sections.SEMANTIC_KEEP", 4)
    plan = plan_interests(extraction(interests=["quán yên tĩnh để làm việc"]))
    key = plan.requests[0].key
    cafes = [poi(f"c{i}", "food.cafe") for i in range(6)]
    pois = {p.id: p for p in [*cafes, poi("bar", "entertainment.nightlife")]}
    hits, close = _semantic_only(key, pois, ["c0", "bar", "c1", "c2", "c3", "c4", "c5"])

    def shown(picked: set[str]) -> list[str]:
        results, _ = assemble(plan, pois, hits, ANY, pinned_ids=frozenset(picked), closeness=close)
        return [i.poi.id for r in results for i in r.items]

    assert set(shown(set())) == {"c0", "c1", "c2", "c3"}
    assert set(shown({"c0", "c1"})) == {"c2", "c3", "c4", "c5"}


def test_the_home_of_a_semantic_section_survives_picking_its_best_places() -> None:
    """N15: 20 museums (the closest) and 10 places of other kinds; 8 museums picked."""
    plan = plan_interests(extraction(interests=["nơi kể về lịch sử chiến tranh"]))
    key = plan.requests[0].key
    others = ["food.restaurant", "entertainment.nightlife", "culture_religion.temple"]
    pois = {
        f"p{n:02d}": poi(f"p{n:02d}", "attraction.museum" if n < 20 else others[n % 3])
        for n in range(30)
    }
    hits, close = _semantic_only(key, pois, list(pois))

    def categories(picked: list[str]) -> set[str]:
        results, _ = assemble(plan, pois, hits, ANY, pinned_ids=frozenset(picked), closeness=close)
        return {i.poi.category for r in results for i in r.items}

    assert categories([]) == {"attraction.museum"}
    assert categories(list(pois)[:8]) == {"attraction.museum"}


def test_defaults_replace_an_other_wish_whose_places_are_all_picked() -> None:
    """N16: the decision uses the pruned hits; no 'not found' for the defaults, and no note
    that the wish found nothing (it did)."""
    wish = {"field": "other", "operator": "prefer", "value": "chỗ không quá đông"}
    plan = plan_interests(extraction(soft_constraints=[wish]))
    key = plan.requests[0].key
    pois = {p.id: p for p in [poi("r"), poi("pagoda", "culture_religion.pagoda"), poi("s1")]}
    hits, close = _semantic_only(key, pois, ["s1"])
    results, warnings = assemble(
        plan, pois, hits, ANY, pinned_ids=frozenset({"s1"}), closeness=close
    )
    assert {r.spec.key: [i.poi.id for i in r.items] for r in results} == {
        "food": ["r"],
        "culture": ["pagoda"],
    }
    assert warnings == []


def test_defaults_replace_an_other_wish_whose_word_hits_are_all_picked() -> None:
    """N16 for places found by words: not sections [] with 'Không tìm thấy … “đi một mình”'."""
    wish = {"field": "other", "operator": "prefer", "value": "đi một mình"}
    pois = [poi("r"), poi("pagoda", "culture_religion.pagoda"), poi("cafe-1", "food.cafe")]
    hits = {"kw:di_mot_minh": {"cafe-1": "text"}}
    plan = plan_interests(extraction(soft_constraints=[wish]))
    by_id = {p.id: p for p in pois}
    results, warnings = assemble(plan, by_id, hits, ANY, pinned_ids=frozenset({"cafe-1"}))
    assert {r.spec.key: [i.poi.id for i in r.items] for r in results} == {
        "food": ["r"],
        "culture": ["pagoda"],
    }
    assert warnings == []
    # Not picked, the place answers the wish.
    results, warnings = assemble(plan, by_id, hits, ANY)
    assert [r.spec.key for r in results] == ["kw:di_mot_minh"] and warnings == []


def test_defaults_name_an_other_wish_whose_places_are_all_closed() -> None:
    """Closed during the trip is not an answer: the defaults say they replace the wish."""
    wish = {"field": "other", "operator": "prefer", "value": "đi một mình"}
    closed = poi("bar-1", "entertainment.nightlife", opening_hours=EVENING, hours_confidence="high")
    pois = [poi("r"), poi("pagoda", "culture_religion.pagoda"), closed]
    morning = TripWindow(None, to_minutes("07:00"), to_minutes("09:00"))
    sections, _, warnings = sections_for(
        pois, {"kw:di_mot_minh": {"bar-1": "text"}}, morning, soft_constraints=[wish]
    )
    assert sections == {"food": ["r"], "culture": ["pagoda"]}
    assert warnings == [NO_MATCH_DEFAULTS.format("đi một mình")]


def test_a_text_past_the_semantic_cap_is_not_reported_as_not_found() -> None:
    """N17: the service warns that it was matched by words only; sections stays silent."""
    plan = plan_interests(extraction(interests=["food", "chỗ k quá du lịch", "phở"]))
    capped, pho = plan.requests[1].key, plan.requests[2].key
    pois = {p.id: p for p in [poi("r"), poi("pho-hung")]}
    # phở is merged into food, but its only place is already picked.
    hits = {capped: {}, pho: {"pho-hung": "name"}}
    picked = frozenset({"pho-hung"})
    both = frozenset({capped, pho})
    assert assemble(plan, pois, hits, ANY, pinned_ids=picked, unsearched=both)[1] == []
    assert assemble(plan, pois, hits, ANY, pinned_ids=picked)[1] == [
        "Không tìm thấy địa điểm cho “chỗ k quá du lịch”",
        "Không tìm thấy địa điểm cho “phở”",
    ]


BARS = [poi(f"bar{i}", "entertainment.nightlife") for i in range(3)]


def test_a_companion_never_takes_its_groups_places() -> None:
    """I1: every bar is also a semantic hit of the words; the cafés and restaurants close in
    meaning have no word hit, so they make no section and the bars stay in nightlife."""
    near = [poi("r1"), poi("r2"), poi("c1", "food.cafe")]
    pois = [*BARS, *near]
    for fields in (
        {"interests": ["pub"]},
        {"hard_constraints": [{"field": "other", "operator": "eq", "value": "kiếm chỗ uống rượu"}]},
    ):
        key = plan_interests(extraction(**fields)).requests[1].key
        hits = {key: {p.id: "semantic" for p in pois}}
        sections, results, warnings = sections_for(pois, hits, **fields)
        assert {k: set(v) for k, v in sections.items()} == {"nightlife": {b.id for b in BARS}}
        assert warnings == [] and results[0].spec.note is None, fields
        assert all(any(h.raw is not None for h in item.hits) for item in results[0].items)


def test_a_companion_outside_its_group_needs_its_words() -> None:
    """Hà Nội "bia": the "Bia hơi …" restaurants carry the word, so they get a section of their
    own (with the restaurants close in meaning); the bars stay in nightlife; a café found by
    meaning only is dropped."""
    halls = [poi("bia-hoi-1", name="Bia hơi 1"), poi("bia-hoi-2", name="Bia hơi 2")]
    pois = [*BARS, *halls, poi("beer-place"), poi("cafe", "food.cafe")]
    hits = {"kw:bia": {"bar0": "text", "bar1": "semantic", "bia-hoi-1": "name", "bia-hoi-2": "name",
                       "beer-place": "semantic", "cafe": "semantic"}}  # fmt: skip
    sections, _, warnings = sections_for(pois, hits, interests=["bia"])
    assert set(sections) == {"nightlife", "kw:bia"}
    assert set(sections["nightlife"]) == {b.id for b in BARS}
    assert sections["nightlife"][:2] in (["bar0", "bar1"], ["bar1", "bar0"])
    assert set(sections["kw:bia"]) == {"bia-hoi-1", "bia-hoi-2", "beer-place"}
    assert warnings == []
    # With food asked for too, the halls are lifted in food instead of a section of their own.
    sections, _, warnings = sections_for(pois, hits, interests=["food", "bia"])
    assert set(sections) == {"food", "nightlife"} and sections["food"][:3] != ["cafe"]
    assert set(sections["food"][:3]) == {"bia-hoi-1", "bia-hoi-2", "beer-place"}
    assert warnings == []


def test_a_companion_that_finds_nothing_is_silent() -> None:
    sections, _, warnings = sections_for(BARS, {"kw:di_bar_voi_ban_be": {}},
                                         interests=["đi bar với bạn bè"])
    assert set(sections) == {"nightlife"} and warnings == []


def test_a_beach_wish_with_more_words_is_answered_by_its_places() -> None:
    wish = {"field": "other", "operator": "prefer", "value": "ăn hải sản ven biển"}
    seafood = [poi("hai-san-1"), poi("hai-san-2")]
    hits = {"kw:an_hai_san_ven_bien": {p.id: "semantic" for p in seafood}}
    sections, _, warnings = sections_for(seafood, hits, soft_constraints=[wish])
    assert sections == {"kw:an_hai_san_ven_bien": ["hai-san-1", "hai-san-2"]}
    assert warnings == []
    sections, _, warnings = sections_for(seafood, {}, soft_constraints=[wish])
    assert sections == {}
    assert warnings == ["Không có dữ liệu cho “ăn hải sản ven biển” ở thành phố này"]
