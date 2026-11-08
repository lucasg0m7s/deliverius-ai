"""Message scenarios: what the customer is about, drawn by the code, never by the LLM.

A scenario fixes the intent (the label), the facts the message may mention, who is writing (the
persona) and which markers the text must carry. The LLM only turns that into words. The facts
signature lets the other-generator test prove that none of its scenarios repeats one of the main
set's.
"""

import hashlib
import json
import random
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.placeholders import PII_MARKERS, Marker
from deliverius.datagen.records import Intent
from deliverius.datagen.rng import IdFactory, Layer, count_for_rate, layer_random
from deliverius.datagen.world import SimRestaurant, generate_restaurants
from deliverius.orders import OrderStatus, PaymentMethod
from deliverius.shared import DeliveriusError

_MAX_REDRAWS: Final = 200
_DELIVERY_FEES: Final = (0, 399, 499, 599, 799)
_LATE_MINUTES: Final = (25, 120)

# Intents with no order behind them: what makes two of them different is the topic.
_NO_ORDER_INTENTS: Final = frozenset({Intent.COUPON_QUESTION, Intent.POLICY_QUESTION, Intent.OTHER})
_AFFECTED_ITEM_INTENTS: Final = frozenset(
    {Intent.ITEM_MISSING, Intent.WRONG_ITEM, Intent.QUALITY_ISSUE}
)
_STATUSES: Final = {
    Intent.LATE_DELIVERY: (OrderStatus.DELIVERED, OrderStatus.OUT_FOR_DELIVERY),
    Intent.NOT_DELIVERED: (OrderStatus.DELIVERED, OrderStatus.DELIVERY_FAILED),
    Intent.CANCEL_ORDER: (OrderStatus.PLACED, OrderStatus.ACCEPTED, OrderStatus.PREPARING),
}
_PAYMENT_METHODS: Final = tuple(PaymentMethod)

# The topics describe the fact in neutral terms, so a provider's safety filter has less to refuse.
_SENSITIVE_TOPICS: Final = (
    "reação alérgica depois de comer um item que o cliente tinha avisado que não podia",
    "mal-estar e vômito depois de comer o pedido",
    "objeto estranho encontrado na comida",
    "comida com cheiro forte, parecendo estragada",
    "entregador foi agressivo com o cliente",
    "entregador fez comentários inadequados ao cliente",
    "entregador sofreu um acidente na frente do cliente",
    "criança comeu algo com um ingrediente que causa alergia",
)
_POLICY_SUBJECTS: Final = (
    "cancelar um pedido",
    "pedido atrasado",
    "item que faltou no pedido",
    "item errado no pedido",
    "problema de qualidade da comida",
    "pedido que não chegou",
    "cobrança duplicada",
    "uso de cupons",
    "alergias e segurança alimentar",
    "conduta do entregador",
    "reembolsos",
    "privacidade dos meus dados",
)
_POLICY_ASKS: Final = (
    "qual é o prazo para reclamar",
    "quanto posso receber de volta",
    "quem analisa e como funciona",
)
_COUPON_SITUATIONS: Final = (
    "o cupom {code} não foi aplicado no pedido",
    "como ganhar um cupom como {code}",
    "o cupom {code} expirou antes de eu usar",
    "dá para somar o cupom {code} com outro cupom",
    "o cupom {code} exige um valor mínimo de pedido",
    "o cupom {code} vale para qualquer restaurante",
    "o cupom {code} pode ser trocado por dinheiro",
    "onde vejo os cupons que tenho na conta, como o {code}",
)
_COUPON_CODES: Final = ("BEMVINDO10", "FRETEGRATIS", "VOLTA15", "SEXTOU20", "PIZZA5", "DOCE10")
_OTHER_BASES: Final = (
    "só um cumprimento, sem pedir nada",
    "agradecimento por um atendimento anterior",
    "pergunta sobre a previsão do tempo",
    "pedido de recomendação de restaurante",
    "pergunta se está falando com uma pessoa ou um robô",
    "pedido de ajuda para trocar a senha do aplicativo",
    "pergunta de como mudar o endereço cadastrado",
    "elogio a um entregador",
    "pergunta sobre como trabalhar como entregador",
    "conversa sem relação com pedidos",
)
_OTHER_MOMENTS: Final = ("de manhã", "à noite", "num fim de semana", "de madrugada")

_TOPICS: Final[dict[Intent, tuple[str, ...]]] = {
    Intent.SENSITIVE: _SENSITIVE_TOPICS,
    Intent.POLICY_QUESTION: tuple(
        f"{subject}: {ask}" for subject in _POLICY_SUBJECTS for ask in _POLICY_ASKS
    ),
    Intent.COUPON_QUESTION: tuple(
        situation.format(code=code) for situation in _COUPON_SITUATIONS for code in _COUPON_CODES
    ),
    Intent.OTHER: tuple(f"{base} ({moment})" for base in _OTHER_BASES for moment in _OTHER_MOMENTS),
}

_SLANG_RATE: Final = 0.35
_TYPOS_RATE: Final = 0.25
_VAGUE_RATE: Final = 0.15


class ScenarioError(DeliveriusError):
    """The scenario draw cannot go on; `rule` is a stable code and `intent` says where."""

    def __init__(self, rule: str, intent: Intent) -> None:
        super().__init__(f"{rule}: {intent}")
        self.rule = rule
        self.intent = intent


class MessageSet(StrEnum):
    """The main set, or the other-generator test, which has its own random stream and role."""

    MAIN = "main"
    HOLDOUT = "holdout"

    @property
    def scenario_layer(self) -> Layer:
        return Layer.SCENARIOS if self is MessageSet.MAIN else Layer.HOLDOUT_SCENARIOS

    @property
    def message_layer(self) -> Layer:
        return Layer.MESSAGES if self is MessageSet.MAIN else Layer.HOLDOUT_MESSAGES


class Tone(StrEnum):
    CALM = "calm"
    ANGRY = "angry"
    ANXIOUS = "anxious"
    POLITE = "polite"
    CURT = "curt"


class Formality(StrEnum):
    FORMAL = "formal"
    NEUTRAL = "neutral"
    INFORMAL = "informal"


class MessageLength(StrEnum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


@dataclass(frozen=True, slots=True)
class Persona:
    tone: Tone
    formality: Formality
    slang: bool
    typos: bool
    vague: bool
    length: MessageLength

    @property
    def label(self) -> str:
        """Stable text for the dataset row: the same persona always gives the same string."""
        return (
            f"tone={self.tone};formality={self.formality};slang={_yes_no(self.slang)};"
            f"typos={_yes_no(self.typos)};vague={_yes_no(self.vague)};length={self.length}"
        )


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


@dataclass(frozen=True, slots=True)
class FactItem:
    name: str
    unit_price_cents: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ScenarioFacts:
    """What the message may mention. `topic` stands in for the order in the intents without one."""

    items: tuple[FactItem, ...]
    total_cents: int | None
    delay_minutes: int | None
    status: OrderStatus | None
    payment_method: PaymentMethod | None
    affected_item: str | None
    topic: str | None

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MessageScenario:
    id: UUID
    intent: Intent
    facts: ScenarioFacts
    persona: Persona
    markers: tuple[Marker, ...]

    @property
    def signature(self) -> str:
        return facts_signature(self.intent, self.facts)


def facts_signature(intent: Intent, facts: ScenarioFacts) -> str:
    """A stable hash of the intent and the facts, ignoring the persona and the identifier."""
    payload = json.dumps(
        {"intent": intent.value, "facts": facts.payload()},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate_message_scenarios(config: DatagenConfig) -> tuple[MessageScenario, ...]:
    """The main set: `scenarios_per_intent` of each of the 11 intents, exact."""
    return _generate(config, MessageSet.MAIN, config.messages.scenarios_per_intent, (), ())


def generate_holdout_scenarios(
    config: DatagenConfig, main: Sequence[MessageScenario]
) -> tuple[MessageScenario, ...]:
    """Scenarios for the other-generator test: no identifier and no facts signature of `main`."""
    return _generate(
        config,
        MessageSet.HOLDOUT,
        config.messages.holdout_scenarios_per_intent,
        tuple(scenario.signature for scenario in main),
        tuple(scenario.id for scenario in main),
    )


@dataclass(frozen=True, slots=True)
class _Draft:
    id: UUID
    intent: Intent
    facts: ScenarioFacts
    persona: Persona


def _generate(
    config: DatagenConfig,
    message_set: MessageSet,
    per_intent: int,
    taken_signatures: Collection[str],
    taken_ids: Collection[UUID],
) -> tuple[MessageScenario, ...]:
    rng = layer_random(config.seed, message_set.scenario_layer)
    ids = IdFactory(rng)
    restaurants = generate_restaurants(config)
    signatures = set(taken_signatures)
    used_ids = set(taken_ids)
    decks = {intent: _Deck(rng, topics) for intent, topics in _TOPICS.items()}
    drafts: list[_Draft] = []
    for intent in Intent:
        for _ in range(per_intent):
            facts = _draw_new_facts(rng, intent, restaurants, decks.get(intent), signatures)
            persona = _draw_persona(rng)
            drafts.append(_Draft(_draw_scenario_id(ids, config, used_ids), intent, facts, persona))
    markers = _assign_markers(rng, config, [draft.intent for draft in drafts])
    return tuple(
        MessageScenario(draft.id, draft.intent, draft.facts, draft.persona, draft_markers)
        for draft, draft_markers in zip(drafts, markers, strict=True)
    )


class _Deck:
    """Topics handed out in a shuffled order without repeating until the pool is spent."""

    def __init__(self, rng: random.Random, pool: tuple[str, ...]) -> None:
        self._rng = rng
        self._pool = pool
        self._cards: list[str] = []

    def draw(self) -> str:
        if not self._cards:
            self._cards = list(self._pool)
            self._rng.shuffle(self._cards)
        return self._cards.pop()


def _draw_scenario_id(ids: IdFactory, config: DatagenConfig, used: set[UUID]) -> UUID:
    identifier = ids.next_id(config.reference_instant)
    while identifier in used:
        identifier = ids.next_id(config.reference_instant)
    used.add(identifier)
    return identifier


def _draw_new_facts(
    rng: random.Random,
    intent: Intent,
    restaurants: Sequence[SimRestaurant],
    deck: _Deck | None,
    signatures: set[str],
) -> ScenarioFacts:
    for _ in range(_MAX_REDRAWS):
        facts = _draw_facts(rng, intent, restaurants, deck)
        signature = facts_signature(intent, facts)
        if signature not in signatures:
            signatures.add(signature)
            return facts
    raise ScenarioError("scenarios.facts_exhausted", intent)


def _draw_facts(
    rng: random.Random, intent: Intent, restaurants: Sequence[SimRestaurant], deck: _Deck | None
) -> ScenarioFacts:
    topic = None if deck is None else deck.draw()
    if intent in _NO_ORDER_INTENTS:
        return ScenarioFacts((), None, None, None, None, None, topic)
    menu = rng.choice(restaurants).menu
    chosen = rng.sample(menu, min(rng.choices((1, 2, 3), weights=(3, 4, 2))[0], len(menu)))
    items = tuple(
        FactItem(entry.name, entry.unit_price.cents, rng.choices((1, 2), weights=(7, 3))[0])
        for entry in chosen
    )
    subtotal = sum(item.unit_price_cents * item.quantity for item in items)
    return ScenarioFacts(
        items=items,
        total_cents=subtotal + rng.choice(_DELIVERY_FEES),
        delay_minutes=rng.randint(*_LATE_MINUTES) if intent is Intent.LATE_DELIVERY else None,
        status=rng.choice(_STATUSES.get(intent, (OrderStatus.DELIVERED,))),
        payment_method=rng.choice(_PAYMENT_METHODS),
        affected_item=rng.choice(items).name if intent in _AFFECTED_ITEM_INTENTS else None,
        topic=topic,
    )


def _draw_persona(rng: random.Random) -> Persona:
    return Persona(
        tone=rng.choice(tuple(Tone)),
        formality=rng.choice(tuple(Formality)),
        slang=rng.random() < _SLANG_RATE,
        typos=rng.random() < _TYPOS_RATE,
        vague=rng.random() < _VAGUE_RATE,
        length=rng.choice(tuple(MessageLength)),
    )


def _assign_markers(
    rng: random.Random, config: DatagenConfig, intents: Sequence[Intent]
) -> list[tuple[Marker, ...]]:
    """Exact shares of the scenarios get personal data (1 or 2 kinds) and an injection attempt.

    An injection never lands on a sensitive scenario: those are handed to a person anyway, and
    mixing the two would blur what the label is about.
    """
    total = len(intents)
    rates = config.messages
    with_pii = rng.sample(range(total), count_for_rate(total, rates.personal_data_rate))
    injectable = [index for index, intent in enumerate(intents) if intent is not Intent.SENSITIVE]
    with_injection = set(
        rng.sample(injectable, min(len(injectable), count_for_rate(total, rates.injection_rate)))
    )
    pii_kinds = {index: set(rng.sample(PII_MARKERS, rng.choice((1, 2)))) for index in with_pii}
    return [
        (
            *(marker for marker in PII_MARKERS if marker in pii_kinds.get(index, ())),
            *((Marker.INJECTION,) if index in with_injection else ()),
        )
        for index in range(total)
    ]
