"""Layer 4: the LLM writes the customer's words, and the code decides everything else.

One call per scenario asks for the variations. The intent written is always the scenario's, even if
the text talks about something else: a vague message also happens in the real world. Every variation
is checked by rules (markers, size, repetition) and the invalid ones are discarded and counted. A
scenario with nothing valid is tried again, up to the configured attempts, each with a new cache
key; the attempt that worked is remembered, so a repeated run starts there.
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

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
from deliverius.datagen.placeholders import (
    Marker,
    PlaceholderFiller,
    markers_in,
    render_marker,
    unknown_markers,
)
from deliverius.datagen.records import Intent, PiiSpan
from deliverius.datagen.rng import IdFactory, layer_random
from deliverius.datagen.scenarios import (
    Formality,
    MessageLength,
    MessageScenario,
    MessageSet,
    Persona,
    Tone,
    generate_holdout_scenarios,
)
from deliverius.llm.prompts import Prompt, load_prompt
from deliverius.llm.roles import ModelRole
from deliverius.orders import OrderStatus, PaymentMethod
from deliverius.privacy import normalize_text
from deliverius.shared import Money

logger = structlog.get_logger(__name__)

PROMPT_VERSION: Final = 1
_PROMPT_NAME_OF: Final = {MessageSet.MAIN: "messages", MessageSet.HOLDOUT: "holdout"}
ROLE_OF: Final = {
    MessageSet.MAIN: ModelRole.DATAGEN,
    MessageSet.HOLDOUT: ModelRole.DATAGEN_HOLDOUT,
}

# What the LLM is told each intent is about. The label never depends on what comes back.
INTENT_BRIEFS: Final = MappingProxyType(
    {
        Intent.ITEM_MISSING: "o cliente reclama que o pedido chegou sem um item que ele pagou",
        Intent.WRONG_ITEM: "o cliente reclama que veio um item diferente do que ele pediu",
        Intent.QUALITY_ISSUE: (
            "o cliente reclama da qualidade da comida (fria, queimada, mal feita, estranha)"
        ),
        Intent.LATE_DELIVERY: "o cliente reclama que o pedido está atrasado ou demorou demais",
        Intent.NOT_DELIVERED: "o cliente diz que o pedido não chegou",
        Intent.BILLING_ISSUE: (
            "o cliente reclama de uma cobrança errada, duplicada ou de valor diferente do combinado"
        ),
        Intent.CANCEL_ORDER: "o cliente quer cancelar o pedido",
        Intent.COUPON_QUESTION: "o cliente pergunta sobre um cupom de desconto",
        Intent.POLICY_QUESTION: "o cliente pergunta como funciona uma regra do aplicativo",
        Intent.SENSITIVE: (
            "o cliente relata um problema de saúde, segurança ou conduta ligado ao pedido ou à"
            " entrega"
        ),
        Intent.OTHER: (
            "o cliente faz um cumprimento ou um comentário fora do atendimento de pedidos"
        ),
    }
)
_TONE_TEXT: Final = {
    Tone.CALM: "calmo",
    Tone.ANGRY: "irritado",
    Tone.ANXIOUS: "ansioso",
    Tone.POLITE: "educado",
    Tone.CURT: "seco",
}
_FORMALITY_TEXT: Final = {
    Formality.FORMAL: "formal",
    Formality.NEUTRAL: "neutra",
    Formality.INFORMAL: "informal",
}
_LENGTH_TEXT: Final = {
    MessageLength.SHORT: "curta (uma ou duas frases)",
    MessageLength.MEDIUM: "média (três a cinco frases)",
    MessageLength.LONG: "longa (um parágrafo)",
}
_STATUS_TEXT: Final = {
    OrderStatus.PLACED: "pedido feito, ainda não aceito pelo restaurante",
    OrderStatus.ACCEPTED: "aceito pelo restaurante",
    OrderStatus.PREPARING: "em preparo",
    OrderStatus.READY_FOR_PICKUP: "pronto para o entregador retirar",
    OrderStatus.OUT_FOR_DELIVERY: "saiu para entrega",
    OrderStatus.DELIVERED: "entregue",
    OrderStatus.CANCELLED: "cancelado",
    OrderStatus.DELIVERY_FAILED: "entrega não concluída",
}
_PAYMENT_TEXT: Final = {
    PaymentMethod.PIX: "Pix",
    PaymentMethod.CREDIT_CARD: "cartão de crédito",
    PaymentMethod.DEBIT_CARD: "cartão de débito",
    PaymentMethod.MEAL_VOUCHER: "vale-refeição",
}


class VariationBatch(BaseModel):
    """What the LLM answers: the texts, still with markers."""

    variations: list[str]


class DiscardReason(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    UNKNOWN_MARKER = "unknown_marker"
    UNREQUESTED_MARKER = "unrequested_marker"
    MISSING_MARKER = "missing_marker"
    DUPLICATE = "duplicate"
    SURPLUS = "surplus"


@dataclass(frozen=True, slots=True)
class WrittenMessage:
    """A valid variation with its markers filled; `intent` is the scenario's."""

    id: UUID
    scenario_id: UUID
    intent: Intent
    text: str
    pii_spans: tuple[PiiSpan, ...]
    has_injection: bool
    persona: str


@dataclass(frozen=True, slots=True)
class MessageGeneration:
    message_set: MessageSet
    scenarios: tuple[MessageScenario, ...]
    messages: tuple[WrittenMessage, ...]
    failed_scenarios: tuple[UUID, ...]
    discards: Mapping[tuple[DiscardReason, Intent], int]

    @property
    def discard_total(self) -> int:
        return sum(self.discards.values())

    def discards_by_intent(self) -> dict[Intent, int]:
        totals: Counter[Intent] = Counter()
        for (_, intent), count in self.discards.items():
            totals[intent] += count
        return dict(totals)


@dataclass(frozen=True, slots=True)
class _Variation:
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class _ScenarioDraft:
    scenario: MessageScenario
    variations: tuple[_Variation, ...]


def check_variation(
    text: str, required: Collection[Marker], max_chars: int
) -> DiscardReason | None:
    """Why a variation cannot be used, or `None`. The length check comes before any pattern."""
    stripped = text.strip()
    if not stripped:
        return DiscardReason.EMPTY
    if len(stripped) > max_chars:
        return DiscardReason.TOO_LONG
    if unknown_markers(stripped):
        return DiscardReason.UNKNOWN_MARKER
    found = set(markers_in(stripped))
    if found - set(required):
        return DiscardReason.UNREQUESTED_MARKER
    if set(required) - found:
        return DiscardReason.MISSING_MARKER
    return None


def render_scenario(scenario: MessageScenario) -> str:
    """The scenario as the delimited block the prompt talks about. Only markers, never values."""
    facts, persona = scenario.facts, scenario.persona
    lines = ["<cenario>", f"situacao: {INTENT_BRIEFS[scenario.intent]}"]
    if facts.topic is not None:
        lines.append(f"assunto: {facts.topic}")
    if facts.items:
        lines.append("pedido:")
        lines += [
            f"- {item.quantity} x {item.name} ({Money(item.unit_price_cents).format_brl()} cada)"
            for item in facts.items
        ]
    if facts.total_cents is not None:
        lines.append(f"total: {Money(facts.total_cents).format_brl()}")
    if facts.affected_item is not None:
        lines.append(f"item_do_problema: {facts.affected_item}")
    if facts.delay_minutes is not None:
        lines.append(f"atraso_em_minutos: {facts.delay_minutes}")
    if facts.status is not None:
        lines.append(f"status_do_pedido: {_STATUS_TEXT[facts.status]}")
    if facts.payment_method is not None:
        lines.append(f"forma_de_pagamento: {_PAYMENT_TEXT[facts.payment_method]}")
    lines += _persona_lines(persona)
    required = ", ".join(render_marker(marker) for marker in scenario.markers) or "nenhum"
    lines += [f"marcadores_obrigatorios: {required}", "</cenario>"]
    return "\n".join(lines)


def _persona_lines(persona: Persona) -> list[str]:
    return [
        "perfil:",
        f"- tom: {_TONE_TEXT[persona.tone]}",
        f"- formalidade: {_FORMALITY_TEXT[persona.formality]}",
        f"- usa_giria: {'sim' if persona.slang else 'não'}",
        f"- erros_de_digitacao: {'sim' if persona.typos else 'não'}",
        f"- mensagem_imprecisa: {'sim' if persona.vague else 'não'}",
        f"- tamanho: {_LENGTH_TEXT[persona.length]}",
    ]


async def generate_messages(
    scenarios: Sequence[MessageScenario],
    models_for_attempt: ModelsForAttempt,
    config: DatagenConfig,
    message_set: MessageSet = MessageSet.MAIN,
    *,
    budget_guard: BudgetGuard | None = None,
    attempt_log: AttemptLog | None = None,
    pause: PauseWatch | None = None,
) -> MessageGeneration:
    """Variations for every scenario, assembled in the order of `scenarios`.

    Raises `BudgetExhaustedError` (nothing is returned) if the quota ends first.
    """
    prompt = load_message_prompt(message_set)
    tally = DiscardTally()
    settings = config.messages

    def accept(
        scenario: MessageScenario, batch: VariationBatch, attempt_tally: DiscardTally
    ) -> _ScenarioDraft | None:
        return _accept(scenario, batch, config, attempt_tally)

    results = await run_jobs(
        _jobs(scenarios, prompt),
        schema=VariationBatch,
        role=ROLE_OF[message_set],
        models_for_attempt=models_for_attempt,
        accept=accept,
        discards=tally,
        concurrency=settings.concurrency,
        max_attempts=settings.max_attempts,
        budget_guard=budget_guard,
        attempt_log=attempt_log,
        pause=pause,
    )
    generation = _assemble(message_set, scenarios, results, config, tally)
    logger.info(
        "datagen.messages_generated",
        message_set=message_set,
        scenarios=len(scenarios),
        messages=len(generation.messages),
        failed=len(generation.failed_scenarios),
        discarded=generation.discard_total,
    )
    return generation


async def generate_holdout_messages(
    config: DatagenConfig,
    models_for_attempt: ModelsForAttempt,
    main: Sequence[MessageScenario],
    *,
    budget_guard: BudgetGuard | None = None,
    attempt_log: AttemptLog | None = None,
    pause: PauseWatch | None = None,
) -> MessageGeneration:
    """The other-generator test: fresh scenarios, another role, another prompt."""
    scenarios = generate_holdout_scenarios(config, main)
    return await generate_messages(
        scenarios,
        models_for_attempt,
        config,
        MessageSet.HOLDOUT,
        budget_guard=budget_guard,
        attempt_log=attempt_log,
        pause=pause,
    )


def load_message_prompt(message_set: MessageSet) -> Prompt:
    return load_prompt(PROMPTS_DIRECTORY, _PROMPT_NAME_OF[message_set], PROMPT_VERSION)


def _jobs(scenarios: Sequence[MessageScenario], prompt: Prompt) -> list[Job[MessageScenario]]:
    return [
        Job(
            key=str(scenario.id),
            payload=scenario,
            messages=[("system", prompt.text), ("human", render_scenario(scenario))],
        )
        for scenario in scenarios
    ]


def _discard(tally: DiscardTally, scenario: MessageScenario, reason: DiscardReason) -> None:
    tally.add(reason.value, scenario.intent.value)
    logger.info("datagen.variation_discarded", scenario_id=str(scenario.id), reason=reason.value)


def _discards_of(tally: DiscardTally) -> Mapping[tuple[DiscardReason, Intent], int]:
    return MappingProxyType(
        {
            (DiscardReason(reason), Intent(intent)): count
            for (reason, intent), count in tally.counts.items()
        }
    )


def _accept(
    scenario: MessageScenario,
    batch: VariationBatch,
    config: DatagenConfig,
    tally: DiscardTally,
) -> _ScenarioDraft | None:
    """The valid variations of one answer, in order; `None` asks for another attempt."""
    settings = config.messages
    kept: list[_Variation] = []
    seen: set[str] = set()
    for index, raw in enumerate(batch.variations):
        text = raw.strip()
        reason: DiscardReason | None
        if index >= settings.variations_per_scenario:
            reason = DiscardReason.SURPLUS
        else:
            reason = check_variation(text, scenario.markers, settings.max_variation_chars)
        key = normalize_text(text)
        if reason is None and key in seen:
            reason = DiscardReason.DUPLICATE
        if reason is not None:
            _discard(tally, scenario, reason)
            continue
        seen.add(key)
        kept.append(_Variation(index, text))
    return _ScenarioDraft(scenario, tuple(kept)) if kept else None


def _assemble(
    message_set: MessageSet,
    scenarios: Sequence[MessageScenario],
    results: Sequence[_ScenarioDraft | None],
    config: DatagenConfig,
    tally: DiscardTally,
) -> MessageGeneration:
    """Scenario by scenario in the given order: repeats across scenarios fall on the later one."""
    filler = PlaceholderFiller(config.seed)
    ids = IdFactory(layer_random(config.seed, message_set.message_layer))
    seen: set[str] = set()
    messages: list[WrittenMessage] = []
    failed: list[UUID] = []
    for scenario, draft in zip(scenarios, results, strict=True):
        if draft is None:
            failed.append(scenario.id)
            continue
        for variation in draft.variations:
            key = normalize_text(variation.text)
            if key in seen:
                _discard(tally, scenario, DiscardReason.DUPLICATE)
                continue
            seen.add(key)
            filled = filler.fill(variation.text, scenario_id=scenario.id, variation=variation.index)
            messages.append(
                WrittenMessage(
                    id=ids.next_id(config.reference_instant),
                    scenario_id=scenario.id,
                    intent=scenario.intent,
                    text=filled.text,
                    pii_spans=filled.pii_spans,
                    has_injection=filled.has_injection,
                    persona=scenario.persona.label,
                )
            )
    return MessageGeneration(
        message_set=message_set,
        scenarios=tuple(scenarios),
        messages=tuple(messages),
        failed_scenarios=tuple(failed),
        discards=_discards_of(tally),
    )
