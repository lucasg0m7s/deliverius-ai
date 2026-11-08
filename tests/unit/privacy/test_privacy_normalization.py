import pytest

from deliverius.privacy import normalize_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "\uff11\uff12\uff13.\uff14\uff15\uff16.\uff17\uff18\uff19-\uff10\uff19",
            "123.456.789-09",
            id="full-width digits",
        ),
        pytest.param("123\u200b.456\u00ad.789-09", "123.456.789-09", id="invisible characters"),
        pytest.param("\u0665\u0662\u0669.\u0669\u0668\u0662", "529.982", id="arabic-indic digits"),
        pytest.param("\u0968\u096a\u096d", "247", id="devanagari digits"),
        pytest.param("\u2460\u2461\u2462", "123", id="circled digits"),
        pytest.param("247\u201325", "247-25", id="en dash"),
        pytest.param("247\u201425", "247-25", id="em dash"),
        pytest.param("247\u221225", "247-25", id="minus sign"),
        pytest.param("a  \t\u00a0 b", "a b", id="horizontal space collapses"),
        pytest.param("a\nb", "a\nb", id="line break stays"),
        pytest.param("n\u00ba 10", "no 10", id="ordinal indicator"),
        pytest.param("pão de açúcar", "pão de açúcar", id="accents stay"),
        pytest.param("", "", id="empty"),
    ],
)
def test_normalization(text: str, expected: str) -> None:
    assert normalize_text(text) == expected


def test_decomposed_accents_are_composed_even_with_an_invisible_character_between() -> None:
    assert normalize_text("a\u200b\u0301") == "\u00e1"
