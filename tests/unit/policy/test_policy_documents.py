import re
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest

from deliverius.policy import (
    POLICY_LIMITS,
    PolicyDocument,
    PolicyDocumentError,
    PolicyDocumentId,
    PolicyLimits,
    format_limit,
    load_policy_documents,
    render_policy_document,
)
from deliverius.shared import Money

DOCUMENTS = load_policy_documents()
# Numbers written by hand: a value in reais, or a number followed by a unit the policy uses.
HAND_WRITTEN_NUMBER = re.compile(r"R\$\s*\d|\d+\s*(%|min|h\b|hora|dia)", re.IGNORECASE)
# Read only for the text of POL-11, never to decide.
TEXT_ONLY_LIMITS = {"instant_refund_credit_time", "card_refund_credit_time"}


def document(document_id: PolicyDocumentId) -> PolicyDocument:
    return next(doc for doc in DOCUMENTS if doc.id is document_id)


def write_documents(directory: Path, overrides: dict[str, str | None]) -> Path:
    """The shipped documents copied as `POL-xx-x.md`; `None` in `overrides` drops a file."""
    for doc in DOCUMENTS:
        name = f"{doc.id}-x.md"
        content = overrides.get(name, doc.markdown)
        if content is not None:
            (directory / name).write_text(content, encoding="utf-8")
    return directory


def test_there_are_exactly_twelve_documents_with_title_and_sections() -> None:
    assert [doc.id for doc in DOCUMENTS] == list(PolicyDocumentId)
    assert all(doc.title and "\n## " in doc.markdown for doc in DOCUMENTS)
    assert document(PolicyDocumentId.POL_03).title == "Item faltando"


def test_filled_pol_03_shows_the_engine_numbers() -> None:
    text = render_policy_document(document(PolicyDocumentId.POL_03), POLICY_LIMITS)
    assert "24 horas" in text
    assert "R$ 50,00" in text
    assert "{{" not in text


def test_filling_follows_the_limits_given() -> None:
    limits = PolicyLimits(auto_approval_limit=Money(7_500))
    assert "R$ 75,00" in render_policy_document(document(PolicyDocumentId.POL_03), limits)


@pytest.mark.parametrize("doc", DOCUMENTS, ids=lambda doc: doc.id)
def test_every_document_fills_without_error(doc: PolicyDocument) -> None:
    assert "{{" not in render_policy_document(doc, POLICY_LIMITS)


@pytest.mark.parametrize("doc", DOCUMENTS, ids=lambda doc: doc.id)
def test_no_document_writes_a_number_by_hand(doc: PolicyDocument) -> None:
    assert HAND_WRITTEN_NUMBER.search(doc.markdown) is None


def test_hand_written_number_pattern_catches_the_usual_forms() -> None:
    for text in ("até R$ 50", "R$50,00", "50%", "24 horas", "24 h", "90 dias", "30 minutos"):
        assert HAND_WRITTEN_NUMBER.search(text), text
    assert HAND_WRITTEN_NUMBER.search("POL-03 e {{auto_approval_limit}}") is None


def test_every_limit_appears_in_some_document() -> None:
    used = {
        name for doc in DOCUMENTS for name in re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", doc.markdown)
    }
    assert {limit.name for limit in fields(PolicyLimits)} <= used
    assert used >= TEXT_ONLY_LIMITS


@pytest.mark.parametrize("document_id", [PolicyDocumentId.POL_09, PolicyDocumentId.POL_10], ids=str)
def test_sensitive_documents_promise_no_refund_and_say_high_priority(
    document_id: PolicyDocumentId,
) -> None:
    text = document(document_id).markdown.lower()
    assert "reembols" not in text
    assert "prioridade alta" in text


def test_unknown_placeholder_names_document_and_placeholder() -> None:
    broken = PolicyDocument(PolicyDocumentId.POL_03, "Item faltando", "# x\n\n{{refund_limit}}")
    with pytest.raises(PolicyDocumentError) as error:
        render_policy_document(broken, POLICY_LIMITS)
    assert error.value.rule == "policy_document.unknown_placeholder"
    assert error.value.document == "POL-03"
    assert error.value.placeholder == "refund_limit"
    assert "POL-03" in str(error.value)
    assert "refund_limit" in str(error.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Money(5_000), "R$ 50,00"),
        (50, "50%"),
        (timedelta(minutes=1), "1 minuto"),
        (timedelta(minutes=30), "30 minutos"),
        (timedelta(minutes=90), "90 minutos"),
        (timedelta(hours=1), "1 hora"),
        (timedelta(hours=24), "24 horas"),
        (timedelta(hours=48), "48 horas"),
        (timedelta(hours=76), "76 horas"),
        (timedelta(days=3), "3 dias"),
        (timedelta(days=90), "90 dias"),
    ],
)
def test_format_limit(value: Money | int | timedelta, expected: str) -> None:
    assert format_limit(value) == expected


def test_loading_from_a_directory_ignores_other_files(tmp_path: Path) -> None:
    write_documents(tmp_path, {})
    (tmp_path / "README.txt").write_text("not a policy", encoding="utf-8")
    assert [doc.id for doc in load_policy_documents(tmp_path)] == list(PolicyDocumentId)


def test_missing_document_fails(tmp_path: Path) -> None:
    write_documents(tmp_path, {"POL-12-x.md": None})
    with pytest.raises(PolicyDocumentError) as error:
        load_policy_documents(tmp_path)
    assert error.value.rule == "policy_document.set_mismatch"


def test_duplicate_document_fails(tmp_path: Path) -> None:
    write_documents(tmp_path, {})
    (tmp_path / "POL-03-copy.md").write_text(document(PolicyDocumentId.POL_03).markdown, "utf-8")
    with pytest.raises(PolicyDocumentError) as error:
        load_policy_documents(tmp_path)
    assert error.value.rule == "policy_document.set_mismatch"


@pytest.mark.parametrize(
    "first_line", ["POL-03 · Item faltando", "# POL-13 · Extra", "# POL-03 - Item faltando"]
)
def test_bad_title_fails(tmp_path: Path, first_line: str) -> None:
    write_documents(tmp_path, {"POL-03-x.md": f"{first_line}\n\n## Prazo\n"})
    with pytest.raises(PolicyDocumentError) as error:
        load_policy_documents(tmp_path)
    assert error.value.rule == "policy_document.bad_title"
    assert error.value.document == "POL-03-x.md"


def test_file_name_must_match_the_title(tmp_path: Path) -> None:
    write_documents(tmp_path, {"POL-03-x.md": "# POL-04 · Item errado\n\n## Prazo\n"})
    with pytest.raises(PolicyDocumentError) as error:
        load_policy_documents(tmp_path)
    assert error.value.rule == "policy_document.file_name_mismatch"


def test_document_needs_a_section(tmp_path: Path) -> None:
    write_documents(tmp_path, {"POL-03-x.md": "# POL-03 · Item faltando\n\nSó texto.\n"})
    with pytest.raises(PolicyDocumentError) as error:
        load_policy_documents(tmp_path)
    assert error.value.rule == "policy_document.no_section"
