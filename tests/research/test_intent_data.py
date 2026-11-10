"""The study reads the checked dataset and gives every approach the application's masked text."""

from pathlib import Path

import pytest
from support.intent_data import message, write_messages

from deliverius.datagen.output import DatasetError
from deliverius.datagen.records import Intent, PiiKind, Split
from deliverius.privacy import MarkerLedger, mask_text, normalize_text
from research.intent.data import INTENTS, LABELS, SENSITIVE, Corpus, load_synthetic, mask

CPF = "529.982.247-25"
EMAIL = "vinicius43@example.net"
PHONE = "(11) 91234-5678"
ADDRESS = "Rua Augusta, 1500"
NAME = "Vinícius Almeida"
WITH_PII = (
    f"Oi, sou {NAME}, meu CPF é {CPF}, e-mail {EMAIL}, telefone {PHONE}, "
    f"moro na {ADDRESS} e o pedido não chegou."
)


def test_the_labels_follow_the_intent_enum_with_sensitive_among_them() -> None:
    assert len(INTENTS) == 11
    assert LABELS[SENSITIVE] == "sensitive"


def test_each_corpus_is_read_and_masked(tmp_path: Path) -> None:
    records = {
        Corpus.TRAIN: [message(WITH_PII, Intent.NOT_DELIVERED)],
        Corpus.VALIDATION: [message("Cadê meu cupom?", Intent.COUPON_QUESTION)],
        Corpus.HOLDOUT: [message("Quero falar com alguém", Intent.SENSITIVE, split=Split.TEST)],
    }

    sets = load_synthetic(write_messages(tmp_path / "data", records))

    assert {corpus: len(sets[corpus]) for corpus in sets} == {
        Corpus.TRAIN: 1,
        Corpus.VALIDATION: 1,
        Corpus.TEST: 0,
        Corpus.HOLDOUT: 1,
    }
    assert sets[Corpus.TRAIN].examples[0].intent is Intent.NOT_DELIVERED
    assert "[CPF_1]" in sets[Corpus.TRAIN].texts[0]
    assert sets[Corpus.HOLDOUT].labels.tolist() == [SENSITIVE]


def test_a_dataset_with_a_wrong_hash_is_refused(tmp_path: Path) -> None:
    folder = write_messages(tmp_path / "data", {Corpus.TRAIN: [message("Meu pedido atrasou")]})
    train = folder / "messages" / "train.jsonl"
    train.write_bytes(train.read_bytes().replace(b"atrasou", b"chegou!"))

    with pytest.raises(DatasetError) as refused:
        load_synthetic(folder)

    assert "dataset.hash_mismatch" in str(refused.value)


def test_the_masked_text_is_what_the_application_gets_whatever_the_key() -> None:
    application_key = b"k" * 32

    expected = mask_text(WITH_PII, MarkerLedger.empty("conversation"), application_key).text

    assert mask(WITH_PII, "message-id") == expected


def test_no_value_of_a_masked_kind_is_left_and_names_are_the_known_limit(tmp_path: Path) -> None:
    record = message(
        WITH_PII,
        spans=[
            (PiiKind.NAME, NAME),
            (PiiKind.CPF, CPF),
            (PiiKind.EMAIL, EMAIL),
            (PiiKind.PHONE, PHONE),
            (PiiKind.ADDRESS, ADDRESS),
        ],
    )

    text = load_synthetic(write_messages(tmp_path / "data", {Corpus.TRAIN: [record]}))[
        Corpus.TRAIN
    ].texts[0]

    left = {
        span.kind
        for span in record.pii_spans
        if normalize_text(record.text[span.start : span.end]) in text
    }
    assert left == {PiiKind.NAME}
