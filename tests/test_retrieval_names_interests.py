from typing import Any

from app.schemas.extraction import Extraction
from app.services.retrieval.config import INTEREST_MAP, KEYWORD_MAP
from app.services.retrieval.interests import contained_group, plan_interests
from app.services.retrieval.names import NameHit, classify
from app.services.retrieval.text import norm

BASE: dict[str, Any] = {
    "destination": "Ho Chi Minh City",
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


def test_norm_strips_accents_case_and_punctuation() -> None:
    assert norm("Phở  Hùng!") == "pho hung"
    assert norm("Đường Đồng Khởi") == "duong dong khoi"
    assert norm("các_món_ăn_huế") == "cac mon an hue"
    assert norm("Bar & Kitchen") == "bar kitchen"
    assert norm(None) == ""


def test_single_exact_hit_resolves() -> None:
    match = classify("Chợ Bến Thành", [NameHit("a", 1.0, True), NameHit("b", 0.7, False)])
    assert match.status == "resolved"
    assert [hit.poi_id for hit in match.hits] == ["a"]


def test_several_exact_hits_are_ambiguous() -> None:
    match = classify("Phở Hùng", [NameHit("a", 1.0, True), NameHit("b", 1.0, True)])
    assert match.status == "ambiguous"
    assert len(match.hits) == 2


def test_clear_fuzzy_winner_resolves_but_close_runner_up_is_ambiguous() -> None:
    assert classify("cho ben tanh", [NameHit("a", 0.72, False), NameHit("b", 0.4, False)]).status == (
        "resolved"
    )
    close = classify("pho", [NameHit("a", 0.9, False), NameHit("b", 0.85, False),
                             NameHit("c", 0.5, False)])
    assert close.status == "ambiguous"
    assert [hit.poi_id for hit in close.hits] == ["a", "b"]


def test_weak_match_is_not_found_and_never_substituted() -> None:
    assert classify("Landmark 81", [NameHit("a", 0.3, False)]).status == "not_found"
    assert classify("Landmark 81", []).hits == ()


def test_standard_interests_with_strength() -> None:
    plan = plan_interests(
        extraction(
            interests=["museum", "food"],
            hard_constraints=[atom("interests", "include", "museum")],
            soft_constraints=[atom("interests", "prefer", "food")],
        )
    )
    assert [(r.group, r.strength) for r in plan.requests] == [
        ("museum", "include"),
        ("food", "prefer"),
    ]


def test_non_standard_interests_map_to_keywords_groups_or_text() -> None:
    plan = plan_interests(
        extraction(interests=["Sao Michelin", "ăn chay", "bar", "phở", "ramen"]),
        cuisines={"ramen": "ramen"},
    )
    by_raw = {r.raw: r for r in plan.requests}
    assert by_raw["Sao Michelin"].keyword is not None
    assert by_raw["Sao Michelin"].keyword.filter == {"michelin": ("star",)}
    assert by_raw["ăn chay"].keyword is not None
    assert by_raw["ăn chay"].keyword.key == "vegetarian"
    assert by_raw["bar"].group == "nightlife"
    assert by_raw["phở"].text == "pho"
    assert by_raw["ramen"].keyword is not None


def test_other_atoms_become_requests_and_duplicates_merge() -> None:
    plan = plan_interests(
        extraction(
            interests=["cafe"],
            hard_constraints=[atom("other", "include", "cà phê"), atom("other", "eq", "michelin")],
            soft_constraints=[atom("interests", "prefer", "cafe")],
        )
    )
    assert [(r.key, r.strength) for r in plan.requests] == [
        ("cafe", "include"),
        ("kw:michelin", "include"),
    ]


def test_avoid_splits_into_nodes_filters_and_names() -> None:
    plan = plan_interests(extraction(avoid=["bảo tàng", "bar", "michelin", "Chợ Bến Thành"]))
    groups = INTEREST_MAP.groups
    assert plan.avoid.nodes == {*groups["museum"].nodes, *groups["nightlife"].nodes}
    assert [k.key for k in plan.avoid.filters] == ["michelin"]
    assert plan.avoid.names == ("Chợ Bến Thành",)


def requests_of(**fields: Any) -> list[tuple[str, str | None, str | None, str, str]]:
    """(key, group, text, strength, source) of each planned request."""
    return [
        (r.key, r.group, r.text, r.strength, r.source)
        for r in plan_interests(extraction(**fields)).requests
    ]


def test_drinking_words_name_the_whole_nightlife_group() -> None:
    for value in ("rượu", "uống rượu", "đi uống rượu", "bar", "giải trí về đêm"):
        assert requests_of(interests=[value]) == [
            ("nightlife", "nightlife", None, "prefer", "interest")
        ], value


def test_a_kind_of_bar_also_searches_the_travellers_words() -> None:
    # Hà Nội "bia": every bar, plus the "Bia hơi …" restaurants found by name.
    assert requests_of(interests=["bia"]) == [
        ("nightlife", "nightlife", None, "prefer", "interest"),
        ("kw:bia", None, "bia", "prefer", "other"),
    ]
    # The hard eq atom makes both include; the first spelling and source are kept.
    assert requests_of(interests=["nhậu"], hard_constraints=[atom("other", "eq", "nhậu")]) == [
        ("nightlife", "nightlife", None, "include", "interest"),
        ("kw:nhau", None, "nhau", "include", "other"),
    ]


def test_a_drinking_wish_filed_as_other_eq_is_searched_inside_the_group() -> None:
    # The exact /chat output for "kiếm chỗ uống rượu": a wish, not a fact.
    assert requests_of(hard_constraints=[atom("other", "eq", "kiếm chỗ uống rượu")]) == [
        ("nightlife", "nightlife", None, "include", "other"),
        ("kw:kiem_cho_uong_ruou", None, "kiem cho uong ruou", "include", "other"),
    ]
    assert requests_of(soft_constraints=[atom("other", "prefer", "chỗ uống rượu")])[0][:2] == (
        "nightlife",
        "nightlife",
    )
    # Typed without accents, the accented alias still matches; a masked "với nhau" leaves it.
    assert requests_of(interests=["kiem cho uong ruou"])[0][1] == "nightlife"
    for value in ("đi nhậu với nhau", "di nhau voi nhau"):
        assert contained_group(value) == "nightlife", value


def test_facts_and_times_make_no_request() -> None:
    for value in ("budget exactly 75 NZD", "available 20:40 to 01:10 the following day",
                  "ngân sách 50 đô", "tối nay", "Tonight"):
        assert requests_of(hard_constraints=[atom("other", "eq", value)]) == [], value
    # A time is never a wish, whatever the operator; "nay" is what is left of "tối nay".
    today = atom("other", "prefer", "hôm nay")
    assert requests_of(interests=["nay"], soft_constraints=[today]) == []
    # An eq alias that holds a digit still names something to look for.
    assert requests_of(hard_constraints=[atom("other", "eq", "1 sao michelin")]) == [
        ("kw:michelin_star", None, None, "include", "other")
    ]
    # "đồ" (thing) is not "đô" (dollar).
    drinks = atom("other", "eq", "đồ uống có cồn")
    assert requests_of(hard_constraints=[drinks])[0][0] == "nightlife"


def test_a_time_inside_a_wish_is_cut_from_its_words() -> None:
    assert requests_of(
        hard_constraints=[atom("other", "eq", "tối nay muốn đi uống rượu với bạn bè")]
    ) == [
        ("nightlife", "nightlife", None, "include", "other"),
        ("kw:muon_di_uong_ruou_voi_ban_be", None, "muon di uong ruou voi ban be", "include",
         "other"),
    ]
    assert requests_of(
        hard_constraints=[atom("other", "eq", "đi nhậu"), atom("other", "eq", "tối nay")]
    ) == [
        ("nightlife", "nightlife", None, "include", "other"),
        ("kw:nhau", None, "nhau", "include", "other"),  # "đi" is a filler word
    ]


def test_a_standard_interest_inside_a_wish_routes_to_its_group() -> None:
    plan = plan_interests(extraction(soft_constraints=[atom("other", "prefer", "cà phê yên tĩnh")]))
    assert [(r.group, r.text) for r in plan.requests] == [("cafe", None), (None, "ca phe yen tinh")]
    assert plan.requests[0].raw == "cà phê yên tĩnh"
    # With "cafe" already asked for, the wish lifts places inside that one section.
    assert [r.key for r in plan_interests(extraction(
        interests=["cafe"], soft_constraints=[atom("other", "prefer", "quán cà phê yên tĩnh")]
    )).requests] == ["cafe", "kw:quan_ca_phe_yen_tinh"]


def test_a_group_without_data_keeps_only_its_request() -> None:
    # No beach in the data: the no-data warning, not restaurants named "Biển".
    for fields in (
        {"interests": ["bãi biển"]},
        {"soft_constraints": [atom("other", "prefer", "bãi biển")]},
    ):
        assert [(r.group, r.text) for r in plan_interests(extraction(**fields)).requests] == [
            ("beach", None)
        ]
    # A bar by the sea is still a bar: the group with data wins over the longer "beach".
    for value in ("beach bar", "bar ven biển"):
        assert requests_of(soft_constraints=[atom("other", "prefer", value)])[0][:2] == (
            "nightlife",
            "nightlife",
        ), value


def test_homographs_and_refusals_do_not_reach_nightlife() -> None:
    for value in ("không uống rượu", "đi chơi cùng nhau", "bìa rừng", "đi chơi cùng nhau cuối tuần",
                  "xem bia tiến sĩ ở Văn Miếu", "ăn cơm rượu", "gà nấu rượu", "barbecue",
                  # Typed without accents, a not-alias still masks its words.
                  "di choi cung nhau", "bia rung"):
        requests = requests_of(soft_constraints=[atom("other", "prefer", value)])
        assert all(group != "nightlife" for _, group, *_ in requests), value
    # A refusal stays a plain text request, which semantic search skips as negated.
    assert requests_of(soft_constraints=[atom("other", "prefer", "không uống rượu")]) == [
        ("kw:khong_uong_ruou", None, "khong uong ruou", "prefer", "other")
    ]


def test_the_longest_contained_alias_wins() -> None:
    assert contained_group("quán bar ven sông") == "nightlife"
    assert contained_group("tìm quán cà phê có bia") == "cafe"
    assert contained_group("bảo tàng lịch sử") == "museum"
    assert contained_group("lịch sử chiến tranh") is None


def test_avoiding_a_bar_by_name_avoids_one_place_not_the_category() -> None:
    plan = plan_interests(extraction(avoid=["Carmen Bar"]))
    assert plan.avoid.nodes == frozenset()
    assert plan.avoid.names == ("Carmen Bar",)
    assert plan.requests == []


def test_keyword_map_keeps_group_aliases_as_written() -> None:
    kind = KEYWORD_MAP.by_alias[norm("nhậu")]
    assert kind.key == "nightlife_kind" and kind.group == "nightlife" and kind.text
    assert "nhậu" in kind.written and "bia thủ công" in kind.written
    generic = KEYWORD_MAP.by_alias[norm("rượu")]
    assert generic.key == "nightlife" and not generic.text and "uống rượu" in generic.written
    assert "tối nay" in KEYWORD_MAP.ignored_wishes
    assert KEYWORD_MAP.version != "keyword_map.v1"


def test_a_negation_after_the_alias_qualifies_the_place() -> None:
    # The negation comes after the alias: still a bar, only a quiet one.
    for value in ("quán bar không ồn ào", "rooftop bar không quá đông",
                  "bar yên tĩnh, không quá ồn", "quán bar ko ồn"):
        requests = requests_of(soft_constraints=[atom("other", "prefer", value)])
        assert requests[0][:2] == ("nightlife", "nightlife"), value
    # Before it, a refusal: no group, the plain text that semantic search skips.
    for value in ("không uống rượu", "tránh xa bar", "don't want a bar", "khong uong ruou"):
        requests = requests_of(soft_constraints=[atom("other", "prefer", value)])
        assert all(group is None for _, group, *_ in requests), value


def test_a_routed_wish_keeps_its_words_as_a_silent_companion() -> None:
    # The stated interest is the group; its words only rank inside it, so finding nothing by
    # words (or no embedder) is not a "not found".
    plan = plan_interests(extraction(interests=["đi bar với bạn bè"]))
    assert [(r.key, r.source, r.companion) for r in plan.requests] == [
        ("nightlife", "interest", None),
        ("kw:di_bar_voi_ban_be", "other", "nightlife"),
    ]
    assert [r.companion for r in plan_interests(extraction(interests=["bia"])).requests] == [
        None,
        "nightlife",
    ]
    # The companion survives the merge of a soft and a hard duplicate.
    hard = [atom("other", "eq", "nhậu")]
    plan = plan_interests(extraction(interests=["nhậu"], hard_constraints=hard))
    assert plan.requests[1].companion == "nightlife" and plan.requests[1].strength == "include"


def test_a_kind_companion_drops_the_filler_words_it_starts_with() -> None:
    # No place is described as "đi nhậu": the words searched (and shown) are "nhậu", so the
    # quán nhậu filed as restaurants still get their own section.
    plan = plan_interests(extraction(hard_constraints=[atom("other", "eq", "đi nhậu")]))
    assert [(r.key, r.raw, r.companion) for r in plan.requests] == [
        ("nightlife", "đi nhậu", None),
        ("kw:nhau", "nhậu", "nightlife"),
    ]
    # Typed without accents, the alias is searched as written: "nhau" would match "cùng nhau".
    for value in ("di nhau", "nhau", "Nhau"):
        plan = plan_interests(extraction(interests=[value]))
        assert [(r.key, r.raw) for r in plan.requests] == [
            ("nightlife", value),
            ("kw:nhau", "nhậu"),
        ], value
    assert plan_interests(extraction(interests=["ruou vang"])).requests[1].raw == "rượu vang"
    assert [r.raw for r in plan_interests(extraction(interests=["uống bia"])).requests] == [
        "uống bia",
        "uống bia",
    ]


def test_a_wish_with_more_than_a_beach_still_searches_its_words() -> None:
    for value in ("ăn hải sản ven biển", "nhà hàng ven biển", "quán hải sản view biển"):
        assert [(r.group, r.companion) for r in plan_interests(extraction(
            soft_constraints=[atom("other", "prefer", value)]
        )).requests] == [("beach", None), (None, "beach")], value
    # Only filler words around the alias: the beach request alone.
    for value in ("bãi biển", "đi biển", "tắm biển", "đi tắm biển", "di tam bien",
                  "go to the beach"):
        assert [r.group for r in plan_interests(extraction(
            soft_constraints=[atom("other", "prefer", value)]
        )).requests] == ["beach"], value


def test_place_words_with_a_currency_word_or_a_number_are_wishes() -> None:
    for value in ("khám phá cố đô", "tham quan cố đô Huế", "dạo quanh thủ đô", "khu đô thị",
                  "ăn ở quận 1", "cà phê 24h"):
        assert requests_of(hard_constraints=[atom("other", "eq", value)]), value
    # Sums, budgets and clock times stay facts.
    for value in ("dưới 200k", "$75 per day", "2 triệu", "budget 75", "9h30 tối"):
        assert requests_of(hard_constraints=[atom("other", "eq", value)]) == [], value


def test_english_plurals_and_phrases_reach_nightlife() -> None:
    for value in ("bars", "drinks", "grab a drink", "go for drinks", "where to drink tonight"):
        assert requests_of(interests=[value]) == [
            ("nightlife", "nightlife", None, "prefer", "interest")
        ], value
    for value in ("pubs", "clubs", "nightclubs", "cocktails", "cocktail bars", "rooftop bars",
                  "sky bars", "wine bars", "beers", "craft beers", "lounges"):
        requests = requests_of(interests=[value])
        key = "kw:" + norm(value).replace(" ", "_")
        assert [r[:2] for r in requests] == [("nightlife", "nightlife"), (key, None)], value
    assert requests_of(interests=["rooftop bars with a view"])[0][:2] == ("nightlife", "nightlife")


def test_refusing_pagodas_or_churches_leaves_out_their_group_not_a_name():
    temples = {"culture_religion.pagoda", "culture_religion.temple", "culture_religion.shrine"}
    for word in ("chua", "chùa", "đền", "đền chùa", "pagodas", "temples"):
        assert plan_interests(extraction(avoid=[word])).avoid.nodes == temples, word
    assert plan_interests(extraction(avoid=["nhà thờ"])).avoid.nodes == {"culture_religion.church"}
    # A dish named with "chua" (sour) is not a pagoda
    assert plan_interests(extraction(avoid=["nem chua"])).avoid.names == ("nem chua",)


def test_unaccented_den_or_chua_inside_a_wish_is_not_a_pagoda():
    for wish in ("muon den mot noi yen tinh", "an canh chua ca loc", "den long hoi an"):
        assert contained_group(wish) is None, wish
    assert contained_group("muốn đi thêm chùa") == "temple"
    assert contained_group("đi viếng đền") == "temple"


def test_the_hanoi_cathedral_is_a_place_not_every_church():
    assert plan_interests(extraction(avoid=["Nhà thờ Lớn"])).avoid.names == ("Nhà thờ Lớn",)
