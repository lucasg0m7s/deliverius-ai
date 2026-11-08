"""Question and policy-section pairs, to evaluate the search over the policies.

The LLM writes questions for one section at a time, from the text already filled with the numbers.
A question that repeats five words in a row from its section is discarded: a keyword search would
find the section by construction, and the evaluation would measure nothing. The label comes from
the section the question was written for.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import structlog
from pydantic import BaseModel

from deliverius.datagen.attempts import AttemptLog
from deliverius.datagen.budget import BudgetGuard
from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.discards import DiscardTally
from deliverius.datagen.generation import (
    PROMPTS_DIRECTORY,
    Job,
    ModelsForAttempt,
    PauseWatch,
    run_jobs,
)
from deliverius.datagen.records import PolicyQaRecord
from deliverius.datagen.rng import IdFactory, Layer, layer_random
from deliverius.llm.prompts import Prompt, load_prompt
from deliverius.llm.roles import ModelRole
from deliverius.policy import (
    POLICY_LIMITS,
    PolicyDocument,
    PolicyDocumentId,
    PolicyLimits,
    load_policy_documents,
    render_policy_document,
)
from deliverius.privacy import normalize_text

logger = structlog.get_logger(__name__)

PROMPT_NAME: Final = "rag_pairs"
PROMPT_VERSION: Final = 1
MAX_QUESTION_CHARS: Final = 300
COPY_RUN_WORDS: Final = 5
_SECTION_MARK: Final = "## "
_WORD: Final = re.compile(r"\w+")


class PairDiscard(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    COPIES_SECTION = "copies_section"
    DUPLICATE = "duplicate"
    SURPLUS = "surplus"


class QuestionBatch(BaseModel):
    questions: list[str]


@dataclass(frozen=True, slots=True)
class PolicySection:
    document: PolicyDocumentId
    document_title: str
    title: str
    text: str

    @property
    def key(self) -> str:
        return f"{self.document}/{self.title}"


@dataclass(frozen=True, slots=True)
class PolicyQaGeneration:
    pairs: tuple[PolicyQaRecord, ...]
    failed_sections: tuple[str, ...]
    discards: Mapping[PairDiscard, int]


@dataclass(frozen=True, slots=True)
class _SectionRequest:
    section: PolicySection
    count: int


@dataclass(frozen=True, slots=True)
class _SectionDraft:
    section: PolicySection
    questions: tuple[str, ...]


def policy_sections(
    documents: Sequence[PolicyDocument] | None = None, limits: PolicyLimits = POLICY_LIMITS
) -> tuple[PolicySection, ...]:
    """The `##` sections of every document, rendered with the numbers, in document order."""
    sections: list[PolicySection] = []
    for document in documents if documents is not None else load_policy_documents():
        rendered = render_policy_document(document, limits)
        sections += _sections_of(document, rendered)
    return tuple(sections)


def shares_a_run_of_words(question: str, section_text: str, run: int = COPY_RUN_WORDS) -> bool:
    """Whether `run` consecutive words of the question appear in a row in the section."""
    section_runs = set(_runs(section_text, run))
    return any(candidate in section_runs for candidate in _runs(question, run))


def questions_per_section(sections: int, target: int) -> list[int]:
    """Spreads `target` questions over `sections` as evenly as whole numbers allow."""
    return [
        (index + 1) * target // sections - index * target // sections for index in range(sections)
    ]


async def generate_rag_pairs(
    models_for_attempt: ModelsForAttempt,
    config: DatagenConfig,
    documents: Sequence[PolicyDocument] | None = None,
    *,
    budget_guard: BudgetGuard | None = None,
    attempt_log: AttemptLog | None = None,
    pause: PauseWatch | None = None,
) -> PolicyQaGeneration:
    """About `rag_pair_target` pairs. Raises `BudgetExhaustedError` if the quota ends first."""
    prompt = load_rag_prompt()
    sections = policy_sections(documents)
    counts = questions_per_section(len(sections), config.messages.rag_pair_target)
    requests = [_SectionRequest(s, n) for s, n in zip(sections, counts, strict=True) if n > 0]
    tally = DiscardTally()

    def accept(
        request: _SectionRequest, batch: QuestionBatch, attempt_tally: DiscardTally
    ) -> _SectionDraft | None:
        return _accept(request, batch, attempt_tally)

    drafts = await run_jobs(
        [_job(request, prompt) for request in requests],
        schema=QuestionBatch,
        role=ModelRole.DATAGEN,
        models_for_attempt=models_for_attempt,
        accept=accept,
        discards=tally,
        concurrency=config.messages.concurrency,
        max_attempts=config.messages.max_attempts,
        budget_guard=budget_guard,
        attempt_log=attempt_log,
        pause=pause,
    )
    generation = _assemble(requests, drafts, config, tally)
    logger.info(
        "datagen.rag_pairs_generated",
        sections=len(requests),
        pairs=len(generation.pairs),
        failed=len(generation.failed_sections),
        discarded=sum(generation.discards.values()),
    )
    return generation


def load_rag_prompt() -> Prompt:
    return load_prompt(PROMPTS_DIRECTORY, PROMPT_NAME, PROMPT_VERSION)


def _sections_of(document: PolicyDocument, rendered: str) -> list[PolicySection]:
    sections: list[PolicySection] = []
    title: str | None = None
    body: list[str] = []

    def close() -> None:
        if title is not None:
            sections.append(
                PolicySection(document.id, document.title, title, "\n".join(body).strip())
            )

    for line in rendered.splitlines():
        if line.startswith(_SECTION_MARK):
            close()
            title, body = line.removeprefix(_SECTION_MARK).strip(), []
        else:
            body.append(line)
    close()
    return sections


def _runs(text: str, run: int) -> list[tuple[str, ...]]:
    words = _WORD.findall(normalize_text(text).casefold())
    return [tuple(words[start : start + run]) for start in range(len(words) - run + 1)]


def _job(request: _SectionRequest, prompt: Prompt) -> Job[_SectionRequest]:
    section = request.section
    block = "\n".join(
        (
            "<secao>",
            f"documento: {section.document} · {section.document_title}",
            f"titulo_da_secao: {section.title}",
            "texto:",
            section.text,
            "</secao>",
            f"quantidade_de_perguntas: {request.count}",
        )
    )
    return Job(
        key=section.key, payload=request, messages=[("system", prompt.text), ("human", block)]
    )


def _accept(
    request: _SectionRequest, batch: QuestionBatch, tally: DiscardTally
) -> _SectionDraft | None:
    kept: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(batch.questions):
        question = raw.strip()
        key = normalize_text(question).casefold()
        reason = _discard_reason(question, key, index, request, seen)
        if reason is not None:
            _discard(tally, request.section, reason)
            continue
        seen.add(key)
        kept.append(question)
    return _SectionDraft(request.section, tuple(kept)) if kept else None


def _discard_reason(
    question: str, key: str, index: int, request: _SectionRequest, seen: set[str]
) -> PairDiscard | None:
    if index >= request.count:
        return PairDiscard.SURPLUS
    if not question:
        return PairDiscard.EMPTY
    if len(question) > MAX_QUESTION_CHARS:
        return PairDiscard.TOO_LONG
    if shares_a_run_of_words(question, request.section.text):
        return PairDiscard.COPIES_SECTION
    if key in seen:
        return PairDiscard.DUPLICATE
    return None


def _discard(tally: DiscardTally, section: PolicySection, reason: PairDiscard) -> None:
    tally.add(reason.value)
    logger.info("datagen.variation_discarded", scenario_id=section.key, reason=reason.value)


def _assemble(
    requests: Sequence[_SectionRequest],
    drafts: Sequence[_SectionDraft | None],
    config: DatagenConfig,
    tally: DiscardTally,
) -> PolicyQaGeneration:
    ids = IdFactory(layer_random(config.seed, Layer.RAG_PAIRS))
    seen: set[str] = set()
    pairs: list[PolicyQaRecord] = []
    failed: list[str] = []
    for request, draft in zip(requests, drafts, strict=True):
        if draft is None:
            failed.append(request.section.key)
            continue
        for question in draft.questions:
            key = normalize_text(question).casefold()
            if key in seen:
                _discard(tally, request.section, PairDiscard.DUPLICATE)
                continue
            seen.add(key)
            pairs.append(
                PolicyQaRecord(
                    id=ids.next_id(config.reference_instant),
                    question=question,
                    document=request.section.document,
                    section=request.section.title,
                )
            )
    counts = {PairDiscard(reason): count for (reason,), count in sorted(tally.counts.items())}
    return PolicyQaGeneration(tuple(pairs), tuple(failed), MappingProxyType(counts))
