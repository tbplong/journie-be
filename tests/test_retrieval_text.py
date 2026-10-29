from app.services.retrieval.names import NameHit, classify
from app.services.retrieval.text import (
    MAX_POSITION,
    TEXT_GAP,
    fold_case,
    has_diacritics,
    lexeme_query,
    lexeme_vector,
)


def test_fold_case_keeps_diacritics_and_drops_case_and_punctuation() -> None:
    assert fold_case("Phở  Hùng!") == "phở hùng"
    assert fold_case("Bar & Kitchen") == "bar kitchen"
    assert fold_case("các_món_ăn_huế") == "các món ăn huế"
    assert fold_case(None) == "" and fold_case("…") == ""
    # NFD input (decomposed accents) is normalized to the same composed form.
    assert fold_case("phở") == fold_case("phở")


def test_has_diacritics() -> None:
    assert has_diacritics("phở") and has_diacritics("đi")
    assert not has_diacritics("pho") and not has_diacritics("123")


def test_vector_stores_written_form_and_folded_form_at_the_same_position() -> None:
    assert lexeme_vector(["Thành phố"]) == "'thành':1A 'thanh':1B 'phố':2A 'pho':2B"
    # A word written without accents is stored once, at weight A only.
    assert lexeme_vector(["Pho Quynh"]) == "'pho':1A 'quynh':2A"


def test_vector_merges_repeats_and_separates_texts() -> None:
    vector = lexeme_vector(["phở bò", None, "", "phở"])
    second_text = 2 + TEXT_GAP + 1
    assert vector == f"'phở':1A,{second_text}A 'pho':1B,{second_text}B 'bò':2A 'bo':2B"
    assert lexeme_vector([]) == "" and lexeme_vector([None, "!!"]) == ""


def test_vector_positions_are_capped() -> None:
    vector = lexeme_vector([" ".join(f"w{i}" for i in range(MAX_POSITION + 5))])
    assert f"'w{MAX_POSITION + 4}':{MAX_POSITION}A" in vector


def test_vector_quotes_lexemes() -> None:
    assert lexeme_vector(["l'été"]) == "'l':1A 'été':2A 'ete':2B"


def test_query_applies_the_accent_rule_per_word() -> None:
    assert lexeme_query("phở") == "('phở':A | 'pho':A)"
    assert lexeme_query("pho") == "'pho':AB"
    # Mixed input: each word follows its own spelling; words must be adjacent.
    assert lexeme_query("Bánh mi") == "('bánh':A | 'banh':A) <-> 'mi':AB"
    assert lexeme_query("") is None and lexeme_query("?!") is None


def test_accent_exact_name_hit_wins_over_folded_only_hits() -> None:
    folded_only = NameHit("pho-street", 1.0, True, exact_marked=False)
    marked = NameHit("pho-soup", 1.0, True, exact_marked=True)
    match = classify("Phở Hùng", [folded_only, marked])
    assert match.status == "resolved" and match.hits == (marked,)
    # Without any accent-exact hit, folded-exact namesakes stay ambiguous.
    other = NameHit("pho-street-2", 1.0, True)
    assert classify("Pho Hung", [folded_only, other]).status == "ambiguous"
