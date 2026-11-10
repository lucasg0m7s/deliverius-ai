"""The human test is read only once it passes the validator, and never overlaps the training."""

from pathlib import Path

import pytest
from support.intent_data import message_set

from deliverius.datagen.records import Intent
from research.intent.data import Corpus, Example, MessageSet
from research.intent.human_test import (
    HumanTestError,
    comparable,
    load_human_test,
    require_no_contamination,
)

HEADER = "id,text,intent,notes\n"


def write_csv(path: Path, per_intent: int, *, extra: str = "") -> Path:
    rows = [
        f'h-{intent}-{n},"Mensagem {n} sobre {intent}",{intent},\n'
        for intent in Intent
        for n in range(per_intent)
    ]
    path.write_text(HEADER + "".join(rows) + extra, encoding="utf-8")
    return path


def test_a_valid_file_is_read_masked_and_hashed(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "human_test.csv", 10)

    human = load_human_test(path, min_per_intent=10)

    assert len(human.messages) == 110
    assert human.messages.corpus is Corpus.HUMAN
    assert human.messages.examples[0] == Example(
        id="h-item_missing-0", text="Mensagem 0 sobre item_missing", intent=Intent.ITEM_MISSING
    )
    assert len(human.sha256) == 64


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HumanTestError) as refused:
        load_human_test(tmp_path / "human_test.csv", min_per_intent=10)

    assert refused.value.rule == "human_test.missing"


def test_a_file_with_personal_data_is_refused_naming_the_line_only(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "human_test.csv",
        10,
        extra='h-x,"Meu CPF é 529.982.247-25 e o pedido sumiu",not_delivered,\n',
    )

    with pytest.raises(HumanTestError) as refused:
        load_human_test(path, min_per_intent=10)

    assert refused.value.rule == "human_test.invalid"
    assert "529" not in str(refused.value)
    assert "112" in str(refused.value)


def test_an_intent_with_fewer_than_the_minimum_is_refused(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "human_test.csv", 9)

    with pytest.raises(HumanTestError) as refused:
        load_human_test(path, min_per_intent=10)

    assert refused.value.rule == "human_test.too_few_per_intent"
    assert "sensitive" in str(refused.value)


def test_a_human_message_also_in_training_is_refused_whatever_the_case_and_spaces() -> None:
    human = MessageSet(
        Corpus.HUMAN,
        (
            Example("h-1", "Meu  pedido NÃO chegou", Intent.NOT_DELIVERED),
            Example("h-2", "Quero um cupom", Intent.COUPON_QUESTION),
        ),
    )
    train = MessageSet(Corpus.TRAIN, (Example("t-1", "meu pedido não chegou", Intent.OTHER),))

    with pytest.raises(HumanTestError) as refused:
        require_no_contamination(human, [train, message_set(Corpus.VALIDATION, 1)])

    assert str(refused.value) == "human_test.contaminated: h-1"


def test_distinct_messages_pass_the_contamination_check() -> None:
    require_no_contamination(
        message_set(Corpus.HUMAN, 2),
        [MessageSet(Corpus.TRAIN, (Example("t", "outra", Intent.OTHER),))],
    )


def test_comparable_text_ignores_case_and_repeated_spaces() -> None:
    assert comparable("  Olá\tMUNDO  ") == comparable("olá mundo")
