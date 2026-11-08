import re
from pathlib import Path

import pytest
from langchain_core.exceptions import ModelAPIError
from structlog.testing import capture_logs
from support.datagen_llm import Answer, DatagenHarness, Responder, harness, structured

from deliverius.datagen import BudgetExhaustedError, DatagenConfig, generate_rag_pairs
from deliverius.datagen.attempts import AttemptLog
from deliverius.datagen.config import MessageConfig
from deliverius.datagen.generation import PROMPTS_DIRECTORY
from deliverius.datagen.rag_pairs import (
    MAX_QUESTION_CHARS,
    PairDiscard,
    PolicySection,
    policy_sections,
    questions_per_section,
    shares_a_run_of_words,
)
from deliverius.llm.errors import ProviderPausedError
from deliverius.llm.fake import Fail
from deliverius.llm.prompts import load_prompt
from deliverius.policy import PolicyDocument, PolicyDocumentId
from deliverius.shared.model_ref import LlmProvider

CONFIG = DatagenConfig()
SECTIONS = policy_sections()
BLOCK = re.compile(
    r"documento: (?P<doc>POL-\d\d) · .*\ntitulo_da_secao: (?P<title>.*)\ntexto:\n"
    r"(?P<text>.*)\n</secao>\nquantidade_de_perguntas: (?P<count>\d+)",
    re.DOTALL,
)


def section_of(human: str) -> tuple[PolicySection, int]:
    match = BLOCK.search(human)
    assert match is not None
    section = next(s for s in SECTIONS if (s.document, s.title) == (match["doc"], match["title"]))
    assert section.text == match["text"]
    return section, int(match["count"])


def own_questions(section: PolicySection, count: int) -> list[str]:
    return [
        f"Pergunta {n} do cliente sobre {section.document} e {section.title}?" for n in range(count)
    ]


def run(tmp_path: Path, respond: Responder) -> DatagenHarness:
    return harness(tmp_path / "cache.sqlite", respond)


def valid_answer(human: str) -> Answer:
    section, count = section_of(human)
    return structured({"questions": own_questions(section, count)})


def test_the_sections_are_the_second_level_headings_of_the_twelve_documents() -> None:
    assert len(SECTIONS) == 48
    assert {s.document for s in SECTIONS} == set(PolicyDocumentId)
    assert [s.title for s in SECTIONS[:4]] == [
        "Quando se aplica",
        "O que o Deliverius faz",
        "Prazo",
        "Quando uma pessoa analisa",
    ]
    assert all(s.text and "{{" not in s.text for s in SECTIONS)


def test_the_text_of_a_section_is_already_filled_with_the_numbers_of_the_policy() -> None:
    deadline = next(s for s in SECTIONS if (s.document, s.title) == ("POL-03", "Prazo"))

    assert "24 horas" in deadline.text
    assert deadline.key == "POL-03/Prazo"


def test_a_document_is_cut_at_its_headings_and_the_text_before_the_first_is_not_a_section() -> None:
    document = PolicyDocument(
        id=PolicyDocumentId.POL_01,
        title="Cancelamento",
        markdown=(
            "# POL-01 · Cancelamento\n\nIntro solta.\n\n"
            "## Um\n\nTexto um.\n\n## Dois\n\nTexto dois.\n"
        ),
    )

    sections = policy_sections([document])

    assert [(s.title, s.text) for s in sections] == [("Um", "Texto um."), ("Dois", "Texto dois.")]
    assert {s.document_title for s in sections} == {"Cancelamento"}


@pytest.mark.parametrize(("sections", "target"), [(48, 60), (5, 3), (7, 7), (10, 95)])
def test_the_questions_are_spread_over_the_sections_and_add_up_to_the_target(
    sections: int, target: int
) -> None:
    counts = questions_per_section(sections, target)

    assert len(counts) == sections
    assert sum(counts) == target
    assert max(counts) - min(counts) <= 1


@pytest.mark.parametrize(
    ("question", "copies"),
    [
        ("Posso reclamar até 24 horas depois da entrega?", True),
        ("Você pode reclamar até 24 horas?", True),
        ("VOCÊ PODE RECLAMAR ATÉ 24 HORAS", True),
        ("Você  pode   reclamar, até: 24 horas!", True),
        ("Posso reclamar até quatro horas depois da entrega?", False),
        ("pode reclamar até 24 mas não mais", False),
        ("Até 24 horas", False),
        ("", False),
    ],
)
def test_five_words_in_a_row_from_the_section_make_a_question_a_copy(
    question: str, copies: bool
) -> None:
    section = "Você pode reclamar até 24 horas depois da entrega."

    assert shares_a_run_of_words(question, section) is copies


def test_the_run_length_can_be_changed() -> None:
    assert shares_a_run_of_words("um dois tres", "xx um dois tres yy", run=3)
    assert not shares_a_run_of_words("um dois tres", "um dois xx tres", run=3)


async def test_each_pair_names_the_document_and_the_section_it_was_written_for(
    tmp_path: Path,
) -> None:
    h = run(tmp_path, valid_answer)

    result = await generate_rag_pairs(h.models(), CONFIG)

    assert len(result.pairs) == 60
    assert result.failed_sections == ()
    assert dict(result.discards) == {}
    labels = {(s.document, s.title) for s in SECTIONS}
    for pair in result.pairs:
        assert (pair.document, pair.section) in labels
        assert pair.document in pair.question
        assert pair.section in pair.question
    assert {pair.document for pair in result.pairs} == set(PolicyDocumentId)
    assert len({pair.id for pair in result.pairs}) == 60
    prompt = load_prompt(PROMPTS_DIRECTORY, "rag_pairs", 1)
    assert {system for system, _ in h.gemini.tracker.prompts} == {prompt.text}


async def test_a_question_that_copies_the_section_is_discarded_and_counted(tmp_path: Path) -> None:
    config = CONFIG.model_copy(update={"messages": MessageConfig(rag_pair_target=2 * 48)})
    target = SECTIONS[2]
    words = target.text.split()
    copy = " ".join(words[:7])

    def respond(human: str) -> Answer:
        section, count = section_of(human)
        if section == target:
            return structured({"questions": [copy, *own_questions(section, count - 1)]})
        return valid_answer(human)

    h = run(tmp_path, respond)

    with capture_logs() as logs:
        result = await generate_rag_pairs(h.models(), config)

    questions = [
        p.question
        for p in result.pairs
        if (p.document, p.section) == (target.document, target.title)
    ]
    assert copy not in questions
    assert result.discards[PairDiscard.COPIES_SECTION] == 1
    [event] = [
        e
        for e in logs
        if e["event"] == "datagen.variation_discarded" and e["reason"] == "copies_section"
    ]
    assert event["scenario_id"] == target.key
    assert copy not in str(logs)


async def test_empty_long_repeated_and_surplus_questions_are_discarded(tmp_path: Path) -> None:
    config = CONFIG.model_copy(update={"messages": MessageConfig(rag_pair_target=4 * 48)})
    first, second = SECTIONS[0], SECTIONS[1]
    shared = "Como faço para falar com uma pessoa do atendimento?"

    def respond(human: str) -> Answer:
        section, count = section_of(human)
        if section == first:
            return structured({"questions": ["", "q" * (MAX_QUESTION_CHARS + 1), shared, shared]})
        if section == second:
            own = own_questions(section, count - 1)
            return structured({"questions": [shared, *own, "uma a mais"]})
        return valid_answer(human)

    h = run(tmp_path, respond)

    result = await generate_rag_pairs(h.models(), config)

    assert dict(result.discards) == {
        PairDiscard.EMPTY: 1,
        PairDiscard.TOO_LONG: 1,
        PairDiscard.DUPLICATE: 2,
        PairDiscard.SURPLUS: 1,
    }
    assert [p.question for p in result.pairs].count(shared) == 1
    assert (first.document, first.title) == next(
        (p.document, p.section) for p in result.pairs if p.question == shared
    )


async def test_a_section_with_no_usable_question_is_reported_after_all_the_attempts(
    tmp_path: Path,
) -> None:
    target = SECTIONS[5]

    def respond(human: str) -> Answer:
        section, _ = section_of(human)
        if section == target:
            return structured({"questions": [""]})
        return valid_answer(human)

    h = run(tmp_path, respond)

    result = await generate_rag_pairs(h.models(), CONFIG)

    assert result.failed_sections == (target.key,)
    assert h.gemini.tracker.calls == len(SECTIONS) - 1 + CONFIG.messages.max_attempts
    assert not any((p.document, p.section) == (target.document, target.title) for p in result.pairs)


async def test_a_repeated_run_after_an_unusable_answer_gives_the_same_pairs_and_discards(
    tmp_path: Path,
) -> None:
    target = SECTIONS[5]
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, "gemini:flash")
    seen = {"first": 0}

    def first_respond(human: str) -> Answer:
        section, _ = section_of(human)
        if section == target:
            seen["first"] += 1
            if seen["first"] == 1:
                return structured({"questions": [""]})
        return valid_answer(human)

    first_run = run(tmp_path, first_respond)
    first = await generate_rag_pairs(first_run.models(), CONFIG, attempt_log=log)
    second_run = run(tmp_path, lambda human: (0.0, Fail(ModelAPIError("not called"))))

    second = await generate_rag_pairs(second_run.models(), CONFIG, attempt_log=log)

    assert dict(first.discards) == {PairDiscard.EMPTY: 1}
    assert second == first
    assert second_run.gemini.tracker.calls == 0


async def test_when_the_quota_ends_the_run_stops_and_says_how_many_sections_are_missing(
    tmp_path: Path,
) -> None:
    config = CONFIG.model_copy(update={"messages": MessageConfig(concurrency=1)})
    calls = {"n": 0}

    def respond(human: str) -> Answer:
        calls["n"] += 1
        if calls["n"] > 10:
            return 0.0, Fail(ProviderPausedError(LlmProvider.GEMINI, 60.0))
        return valid_answer(human)

    h = run(tmp_path, respond)

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_rag_pairs(h.models(), config)

    assert stopped.value.reason.value == "provider_paused"
    asked = len([c for c in questions_per_section(48, 60) if c])
    assert stopped.value.remaining == asked - 10


async def test_a_target_below_the_number_of_sections_asks_only_the_sections_that_got_a_question(
    tmp_path: Path,
) -> None:
    config = CONFIG.model_copy(update={"messages": MessageConfig(rag_pair_target=10)})
    h = run(tmp_path, valid_answer)

    result = await generate_rag_pairs(h.models(), config)

    assert h.gemini.tracker.calls == 10
    assert len(result.pairs) == 10
