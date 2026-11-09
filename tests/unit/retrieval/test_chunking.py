"""Chunks by section: citable IDs, the title in the text, the hash of the rendered document."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from deliverius.policy import (
    POLICY_LIMITS,
    PolicyDocument,
    PolicyDocumentId,
    load_policy_documents,
    render_policy_document,
)
from deliverius.retrieval.chunking import (
    ChunkedDocument,
    ChunkingError,
    chunk_document,
    chunk_policy_documents,
    section_slug,
)

DOCUMENTS = load_policy_documents()


def chunked(document_id: PolicyDocumentId) -> ChunkedDocument:
    return next(doc for doc in chunk_policy_documents() if doc.document_id == document_id)


def make_document(markdown: str) -> PolicyDocument:
    return PolicyDocument(PolicyDocumentId.POL_03, "Item faltando", markdown)


def test_every_document_has_four_sections_in_order() -> None:
    documents = chunk_policy_documents()

    assert [doc.document_id for doc in documents] == [str(d.id) for d in DOCUMENTS]
    assert {len(doc.chunks) for doc in documents} == {4}
    assert [c.position for c in documents[0].chunks] == [1, 2, 3, 4]


def test_the_section_deadline_of_the_missing_item_policy_is_citable() -> None:
    chunk = next(c for c in chunked(PolicyDocumentId.POL_03).chunks if c.section == "Prazo")

    assert chunk.chunk_id == "POL-03#prazo"
    assert chunk.document_id == "POL-03"


def test_the_text_starts_with_the_titles_of_the_document_and_the_section() -> None:
    chunk = next(c for c in chunked(PolicyDocumentId.POL_03).chunks if c.section == "Prazo")

    assert chunk.content.startswith("Item faltando · Prazo\n\n")
    assert "Você pode reclamar até 24 horas depois da entrega." in chunk.content


def test_a_section_text_holds_only_its_own_body() -> None:
    chunks = chunked(PolicyDocumentId.POL_03).chunks
    deadline = next(c for c in chunks if c.section == "Prazo")

    assert "Cada item só pode ser reclamado" not in deadline.content


def test_no_placeholder_is_left_in_any_chunk() -> None:
    for document in chunk_policy_documents():
        for chunk in document.chunks:
            assert "{{" not in chunk.content, chunk.chunk_id


def test_chunk_ids_are_unique_across_all_documents() -> None:
    ids = [c.chunk_id for doc in chunk_policy_documents() for c in doc.chunks]

    assert len(ids) == len(set(ids)) == 48


def test_the_hash_does_not_change_between_runs() -> None:
    assert chunk_policy_documents() == chunk_policy_documents()


def test_the_hash_covers_the_model_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {doc.document_id: doc.document_hash for doc in chunk_policy_documents()}
    monkeypatch.setattr("deliverius.retrieval.chunking.MODEL_REVISION", "another-revision")

    after = {doc.document_id: doc.document_hash for doc in chunk_policy_documents()}

    assert before.keys() == after.keys()
    assert all(before[doc_id] != after[doc_id] for doc_id in before)


def test_the_hash_is_not_the_bare_hash_of_the_rendered_document() -> None:
    document = DOCUMENTS[0]
    rendered = render_policy_document(document, POLICY_LIMITS)

    assert (
        chunk_document(document, POLICY_LIMITS).document_hash
        != hashlib.sha256(rendered.encode()).hexdigest()
    )


def test_a_limit_changes_only_the_hash_of_the_documents_that_show_it() -> None:
    longer = replace(POLICY_LIMITS, item_claim_window=timedelta(hours=60))
    before = {doc.document_id: doc.document_hash for doc in chunk_policy_documents()}
    after = {doc.document_id: doc.document_hash for doc in chunk_policy_documents(limits=longer)}
    citing = {str(doc.id) for doc in DOCUMENTS if "item_claim_window" in doc.markdown}

    changed = {doc_id for doc_id in before if before[doc_id] != after[doc_id]}

    assert changed == citing
    assert citing == {"POL-03", "POL-04", "POL-05"}


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Prazo", "prazo"),
        ("Quando se aplica", "quando-se-aplica"),
        ("O que o Deliverius faz", "o-que-o-deliverius-faz"),
        ("Cobrança em duplicidade", "cobranca-em-duplicidade"),
        ("  Ação & reação!  ", "acao-reacao"),
        ("ÇÃO", "cao"),
    ],
)
def test_the_slug_is_lowercase_without_accents_and_with_hyphens(title: str, slug: str) -> None:
    assert section_slug(title) == slug


def test_a_repeated_section_title_is_refused() -> None:
    markdown = "# POL-03 · Item faltando\n\n## Prazo\n\nUm.\n\n## Prazo\n\nDois.\n"

    with pytest.raises(ChunkingError) as error:
        chunk_document(make_document(markdown), POLICY_LIMITS)

    assert error.value.rule == "chunking.duplicate_section"
    assert (error.value.document, error.value.section) == ("POL-03", "Prazo")


def test_two_titles_with_the_same_slug_are_refused() -> None:
    markdown = "# POL-03 · Item faltando\n\n## Prazo\n\nUm.\n\n## PRAZO!\n\nDois.\n"

    with pytest.raises(ChunkingError) as error:
        chunk_document(make_document(markdown), POLICY_LIMITS)

    assert error.value.rule == "chunking.duplicate_section"


def test_a_title_without_letters_or_digits_is_refused() -> None:
    markdown = "# POL-03 · Item faltando\n\n## ???\n\nUm.\n"

    with pytest.raises(ChunkingError) as error:
        chunk_document(make_document(markdown), POLICY_LIMITS)

    assert error.value.rule == "chunking.empty_slug"


def test_text_before_the_first_section_is_not_a_chunk() -> None:
    markdown = "# POL-03 · Item faltando\n\nIntrodução solta.\n\n## Prazo\n\nUm.\n"

    document = chunk_document(make_document(markdown), POLICY_LIMITS)

    assert [c.chunk_id for c in document.chunks] == ["POL-03#prazo"]
    assert "Introdução" not in document.chunks[0].content
