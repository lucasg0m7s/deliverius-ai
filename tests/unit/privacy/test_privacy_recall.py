import pytest
from support.personal_data_samples import (
    LIMIT_SOURCES,
    STRUCTURED_SOURCES,
    ValueSource,
    is_fully_hidden,
    measure_recall,
)

RECALL_FLOOR = 0.99


@pytest.mark.parametrize(("kind", "source"), STRUCTURED_SOURCES.items(), ids=STRUCTURED_SOURCES)
def test_structured_kinds_stay_above_the_floor(kind: str, source: ValueSource) -> None:
    recall = measure_recall(source)

    assert recall >= RECALL_FLOOR, f"recall of {kind} fell to {recall:.2%}"


def test_person_names_are_a_known_limit_not_detected_at_all() -> None:
    """Published in `docs/seguranca.md`. A rise here means the document needs a new number."""
    assert measure_recall(LIMIT_SOURCES["person_name"]) == 0


def test_only_a_partly_hidden_value_does_not_count_as_hidden() -> None:
    """`Rua 12` is recognized, but `de Outubro` stays visible."""
    assert not is_fully_hidden("moro na rua 12 de outubro", "rua 12 de outubro")
