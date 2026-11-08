import re
from uuid import UUID

import pytest

from deliverius.datagen.placeholders import (
    ANY_MARKER,
    INJECTION_ATTACKS,
    KNOWN_MARKER,
    PII_KIND_OF,
    PII_MARKERS,
    Marker,
    PlaceholderFiller,
    markers_in,
    render_marker,
    unknown_markers,
)
from deliverius.datagen.records import PiiKind
from deliverius.privacy import find_personal_data
from deliverius.privacy.documents import is_valid_cpf

SCENARIO = UUID(int=1)
OTHER_SCENARIO = UUID(int=2)


def test_the_known_pattern_is_exactly_the_six_markers() -> None:
    assert KNOWN_MARKER.pattern == r"\[\[(NOME|CPF|TELEFONE|EMAIL|ENDERECO|INJECAO)\]\]"
    alternatives = re.search(r"\((.*)\)", KNOWN_MARKER.pattern)
    assert alternatives is not None
    assert {marker.value for marker in Marker} == set(alternatives.group(1).split("|"))
    for marker in Marker:
        assert KNOWN_MARKER.fullmatch(render_marker(marker))


def test_markers_are_found_in_order_with_repeats() -> None:
    text = "oi [[NOME]], meu cpf é [[CPF]] e de novo [[NOME]]"

    assert markers_in(text) == (Marker.NAME, Marker.CPF, Marker.NAME)


@pytest.mark.parametrize(
    "text",
    ["[[CNPJ]]", "[[cpf]]", "[[ ]]", "[[NOME ]]", "[[[NOME]]", "[[NOME]] e [[RG]]"],
)
def test_an_unknown_marker_is_detected(text: str) -> None:
    assert unknown_markers(text)


def test_known_markers_are_not_reported_as_unknown() -> None:
    assert unknown_markers("[[NOME]] [[CPF]] [[TELEFONE]] [[EMAIL]] [[ENDERECO]] [[INJECAO]]") == ()
    assert unknown_markers("sem nada de colchetes, só [um] e [[]]") == ()


def test_the_unknown_pattern_is_bounded_so_a_long_run_costs_little() -> None:
    hostile = "[[" + "a" * 5_000

    assert ANY_MARKER.search(hostile) is None
    assert unknown_markers("[[" + "a" * 21 + "]]") == ()


def test_the_fill_puts_each_value_where_the_marker_was_and_records_the_position() -> None:
    template = "Oi, sou [[NOME]], cpf [[CPF]], fone [[TELEFONE]], [[EMAIL]], moro em [[ENDERECO]]."

    filled = PlaceholderFiller(42).fill(template, scenario_id=SCENARIO, variation=0)

    assert "[[" not in filled.text
    assert [span.kind for span in filled.pii_spans] == [
        PiiKind.NAME,
        PiiKind.CPF,
        PiiKind.PHONE,
        PiiKind.EMAIL,
        PiiKind.ADDRESS,
    ]
    values = {span.kind: filled.text[span.start : span.end] for span in filled.pii_spans}
    assert is_valid_cpf("".join(char for char in values[PiiKind.CPF] if char.isdigit()))
    assert "@" in values[PiiKind.EMAIL]
    assert filled.text == (
        f"Oi, sou {values[PiiKind.NAME]}, cpf {values[PiiKind.CPF]}, "
        f"fone {values[PiiKind.PHONE]}, {values[PiiKind.EMAIL]}, "
        f"moro em {values[PiiKind.ADDRESS]}."
    )
    assert not filled.has_injection


def test_the_masking_layer_finds_the_cpf_exactly_where_the_span_says() -> None:
    filled = PlaceholderFiller(7).fill("cpf [[CPF]] ok", scenario_id=SCENARIO, variation=3)

    [span] = filled.pii_spans
    [match] = [m for m in find_personal_data(filled.text) if m.kind.value == "CPF"]
    assert (match.start, match.end) == (span.start, span.end)


def test_a_repeated_marker_gets_the_same_value_and_each_position() -> None:
    filled = PlaceholderFiller(1).fill("[[NOME]] e [[NOME]]", scenario_id=SCENARIO, variation=0)

    first, second = filled.pii_spans
    assert filled.text[first.start : first.end] == filled.text[second.start : second.end]
    assert first.end < second.start


def test_an_injection_marker_becomes_an_attack_of_the_catalog_and_has_no_span() -> None:
    filled = PlaceholderFiller(5).fill(
        "meu pedido atrasou. [[INJECAO]] obrigado", scenario_id=SCENARIO, variation=2
    )

    assert filled.has_injection
    assert filled.pii_spans == ()
    assert any(attack in filled.text for attack in INJECTION_ATTACKS)
    assert filled.text.startswith("meu pedido atrasou. ")


def test_a_text_without_markers_is_left_alone() -> None:
    filled = PlaceholderFiller(5).fill("só uma reclamação", scenario_id=SCENARIO, variation=0)

    assert (filled.text, filled.pii_spans, filled.has_injection) == ("só uma reclamação", (), False)


def test_the_values_depend_on_the_scenario_and_variation_and_not_on_the_order_of_calls() -> None:
    template = "[[NOME]] [[CPF]] [[INJECAO]]"
    forward = PlaceholderFiller(42)
    backward = PlaceholderFiller(42)

    first = [
        forward.fill(template, scenario_id=s, variation=v)
        for s in (SCENARIO, OTHER_SCENARIO)
        for v in range(3)
    ]
    keys = [(s, v) for s in (SCENARIO, OTHER_SCENARIO) for v in range(3)]
    second = [backward.fill(template, scenario_id=s, variation=v) for s, v in reversed(keys)]

    assert first == list(reversed(second))
    assert len({filled.text for filled in first}) == 6


def test_another_seed_gives_other_values() -> None:
    template = "[[NOME]] [[CPF]]"

    a = PlaceholderFiller(1).fill(template, scenario_id=SCENARIO, variation=0)
    b = PlaceholderFiller(2).fill(template, scenario_id=SCENARIO, variation=0)

    assert a.text != b.text


def test_every_personal_marker_has_a_kind() -> None:
    assert set(PII_KIND_OF) == set(PII_MARKERS)
    assert set(PII_KIND_OF.values()) == set(PiiKind)


def test_the_attack_catalog_has_thirty_distinct_attacks_that_are_not_markers() -> None:
    assert len(INJECTION_ATTACKS) == 30
    assert len(set(INJECTION_ATTACKS)) == 30
    assert not any(ANY_MARKER.search(attack) for attack in INJECTION_ATTACKS)
    assert all(re.search(r"[a-zç]", attack) for attack in INJECTION_ATTACKS)
