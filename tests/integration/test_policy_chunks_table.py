"""The search configuration and the `policy_chunks` table: accents, stems, generated column, GIN."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deliverius.db.tables import PolicyChunkRow

pytestmark = pytest.mark.integration

DIMENSIONS = 384
CONTENTS = [
    "Reembolso. O pedido que não chegou é reembolsado em até 24 horas.",
    "Cancelamento. O cliente pode cancelar enquanto o restaurante não aceitou.",
    "Cupom. Cada cupom vale para um único pedido e expira em sete dias.",
    "Item faltando. O prazo para reclamar de item faltando é de 48 horas.",
    "Pagamento. O pix é estornado na hora e o cartão em até cinco dias.",
    "Entrega. O entregador espera o cliente por dez minutos no endereço.",
    "Alergia. Informe a alergia no pedido para o restaurante avisar.",
    "Conta. Para trocar a senha, use a opção esquecei a senha na entrada.",
]


def chunk_values(index: int, content: str) -> dict[str, object]:
    embedding = [0.0] * DIMENSIONS
    embedding[index] = 1.0
    return {
        "chunk_id": f"POL-{index:02d}#section",
        "document_id": f"POL-{index:02d}",
        "section": "Section",
        "position": 1,
        "content": content,
        "document_hash": "0" * 64,
        "embedding": embedding,
        "indexed_at": datetime.now(UTC),
    }


async def insert_chunks(session: AsyncSession, contents: list[str]) -> None:
    await session.execute(
        insert(PolicyChunkRow), [chunk_values(i, content) for i, content in enumerate(contents)]
    )


async def matches(session: AsyncSession, document: str, query: str) -> bool:
    result = await session.execute(
        text(
            "SELECT to_tsvector('pt_unaccent', :document) "
            "@@ websearch_to_tsquery('pt_unaccent', :q)"
        ),
        {"document": document, "q": query},
    )
    return bool(result.scalar_one())


async def test_a_word_without_accent_finds_the_accented_word(session: AsyncSession) -> None:
    assert await matches(session, "O pedido não chegou", "nao chegou")


async def test_an_accented_query_finds_an_unaccented_text(session: AsyncSession) -> None:
    assert await matches(session, "O pedido nao chegou", "não chegou")


async def test_a_stem_finds_other_forms_of_the_word(session: AsyncSession) -> None:
    assert await matches(session, "Cancelamento do pedido", "cancelar")


async def test_an_unrelated_word_does_not_match(session: AsyncSession) -> None:
    assert not await matches(session, "O pedido não chegou", "senha")


@pytest.mark.parametrize("query", ["!", "&", "|", ":*", '"aberta', "chegou &", "a:*b"])
async def test_operators_in_a_query_never_cause_a_syntax_error(
    session: AsyncSession, query: str
) -> None:
    await matches(session, "O pedido não chegou", query)


async def test_the_tsvector_is_generated_from_the_content(session: AsyncSession) -> None:
    await insert_chunks(session, CONTENTS[:1])

    found = await session.execute(
        text(
            "SELECT chunk_id FROM policy_chunks "
            "WHERE tsv @@ websearch_to_tsquery('pt_unaccent', 'nao chegou')"
        )
    )

    assert found.scalars().all() == ["POL-00#section"]


async def test_the_embedding_round_trips_with_384_dimensions(session: AsyncSession) -> None:
    await insert_chunks(session, CONTENTS[:2])

    row = (
        await session.execute(
            select(PolicyChunkRow).where(PolicyChunkRow.chunk_id == "POL-01#section")
        )
    ).scalar_one()

    assert len(row.embedding) == DIMENSIONS
    assert row.embedding[1] == 1.0


async def test_an_embedding_of_another_size_is_refused(session: AsyncSession) -> None:
    values = chunk_values(0, CONTENTS[0]) | {"embedding": [0.0] * (DIMENSIONS - 1)}

    with pytest.raises(DBAPIError, match="dimensions"):
        async with session.begin_nested():
            await session.execute(insert(PolicyChunkRow).values(**values))


async def test_a_chunk_id_cannot_repeat(session: AsyncSession) -> None:
    await insert_chunks(session, CONTENTS[:1])

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await insert_chunks(session, CONTENTS[:1])


async def test_the_lexical_search_uses_the_gin_index(session: AsyncSession) -> None:
    await insert_chunks(session, CONTENTS)
    await session.execute(text("ANALYZE policy_chunks"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await session.execute(
        text(
            "EXPLAIN SELECT chunk_id FROM policy_chunks "
            "WHERE tsv @@ websearch_to_tsquery('pt_unaccent', 'nao chegou')"
        )
    )

    assert "ix_policy_chunks_tsv" in "\n".join(plan.scalars().all())


async def test_the_document_filter_uses_its_index(session: AsyncSession) -> None:
    await insert_chunks(session, CONTENTS)
    await session.execute(text("ANALYZE policy_chunks"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await session.execute(
        text("EXPLAIN SELECT chunk_id FROM policy_chunks WHERE document_id = 'POL-03'")
    )

    assert "ix_policy_chunks_document_id" in "\n".join(plan.scalars().all())
