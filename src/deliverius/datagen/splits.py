"""Splits that do not leak: the antifraude history is cut by time, never shuffled, and the
messages are divided by scenario, so the variations of one scenario stay on the same side."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from deliverius.datagen.messages import MessageGeneration, WrittenMessage
from deliverius.datagen.records import ClaimRecord, Intent, MessageRecord, Split
from deliverius.datagen.rng import Layer, count_for_rate, layer_random
from deliverius.datagen.scenarios import MessageScenario, ScenarioError

# Validation and test get 15% of each intent; the rest, 70%, is the training side.
_HELD_OUT_RATE: Final = 0.15
_MIN_SCENARIOS_PER_INTENT: Final = 3


@dataclass(frozen=True, slots=True)
class ClaimSplit:
    train: tuple[ClaimRecord, ...]
    test: tuple[ClaimRecord, ...]


def split_claims_by_time(claims: Sequence[ClaimRecord], cutoff: datetime) -> ClaimSplit:
    """Train is strictly before `cutoff`; test is `cutoff` and after."""
    return ClaimSplit(
        train=tuple(claim for claim in claims if claim.claimed_at < cutoff),
        test=tuple(claim for claim in claims if claim.claimed_at >= cutoff),
    )


def split_scenarios(scenarios: Iterable[MessageScenario], seed: int) -> dict[UUID, Split]:
    """70/15/15 by scenario, stratified by intent: every intent has all three sides."""
    rng = layer_random(seed, Layer.SPLITS)
    pool = list(scenarios)
    sides: dict[UUID, Split] = {}
    for intent in Intent:
        identifiers = [scenario.id for scenario in pool if scenario.intent is intent]
        if len(identifiers) < _MIN_SCENARIOS_PER_INTENT:
            raise ScenarioError("splits.too_few_scenarios", intent)
        rng.shuffle(identifiers)
        held_out = max(1, count_for_rate(len(identifiers), _HELD_OUT_RATE))
        for position, identifier in enumerate(identifiers):
            if position < held_out:
                sides[identifier] = Split.TEST
            elif position < 2 * held_out:
                sides[identifier] = Split.VALIDATION
            else:
                sides[identifier] = Split.TRAIN
    return sides


def split_message_records(generation: MessageGeneration, seed: int) -> tuple[MessageRecord, ...]:
    """The main set as dataset rows. A scenario with no message left takes no part in the split."""
    with_messages = {message.scenario_id for message in generation.messages}
    sides = split_scenarios(
        (scenario for scenario in generation.scenarios if scenario.id in with_messages), seed
    )
    return tuple(_record(message, sides[message.scenario_id]) for message in generation.messages)


def holdout_message_records(generation: MessageGeneration) -> tuple[MessageRecord, ...]:
    """The other-generator test is all test."""
    return tuple(_record(message, Split.TEST) for message in generation.messages)


def _record(message: WrittenMessage, split: Split) -> MessageRecord:
    return MessageRecord(
        id=message.id,
        scenario_id=message.scenario_id,
        split=split,
        text=message.text,
        intent=message.intent,
        pii_spans=message.pii_spans,
        has_injection=message.has_injection,
        persona=message.persona,
    )
