from collections import Counter, defaultdict
from types import MappingProxyType
from uuid import UUID

import pytest

from deliverius.datagen import (
    DatagenConfig,
    MessageGeneration,
    MessageScenario,
    MessageSet,
    WrittenMessage,
    generate_message_scenarios,
    split_message_records,
    split_scenarios,
)
from deliverius.datagen.config import MessageConfig
from deliverius.datagen.records import Intent, Split
from deliverius.datagen.scenarios import ScenarioError

SEED = 42
SCENARIOS = generate_message_scenarios(DatagenConfig())


def generation_of(
    scenarios: tuple[MessageScenario, ...], per_scenario: int = 10
) -> MessageGeneration:
    messages = tuple(
        WrittenMessage(
            id=UUID(int=(index + 1) * 1_000 + n),
            scenario_id=scenario.id,
            intent=scenario.intent,
            text=f"{scenario.id} {n}",
            pii_spans=(),
            has_injection=False,
            persona=scenario.persona.label,
        )
        for index, scenario in enumerate(scenarios)
        for n in range(per_scenario)
    )
    return MessageGeneration(MessageSet.MAIN, scenarios, messages, (), MappingProxyType({}))


def test_a_scenario_never_has_messages_on_two_sides() -> None:
    records = split_message_records(generation_of(SCENARIOS), SEED)

    sides: defaultdict[UUID, set[Split]] = defaultdict(set)
    for record in records:
        sides[record.scenario_id].add(record.split)
    assert len(sides) == len(SCENARIOS)
    assert all(len(side) == 1 for side in sides.values())
    assert len(records) == len(SCENARIOS) * 10


def test_every_intent_is_on_the_three_sides() -> None:
    records = split_message_records(generation_of(SCENARIOS), SEED)

    seen = {(record.intent, record.split) for record in records}
    assert seen == {(intent, split) for intent in Intent for split in Split}


def test_the_split_is_seventy_fifteen_fifteen_by_scenario_in_each_intent() -> None:
    sides = split_scenarios(SCENARIOS, SEED)

    per_intent: defaultdict[Intent, Counter[Split]] = defaultdict(Counter)
    for scenario in SCENARIOS:
        per_intent[scenario.intent][sides[scenario.id]] += 1
    assert all(
        counts == {Split.TRAIN: 19, Split.VALIDATION: 4, Split.TEST: 4}
        for counts in per_intent.values()
    )


def test_the_split_is_deterministic_and_follows_the_seed() -> None:
    assert split_scenarios(SCENARIOS, SEED) == split_scenarios(SCENARIOS, SEED)
    assert split_scenarios(SCENARIOS, SEED) != split_scenarios(SCENARIOS, SEED + 1)


def test_the_order_of_the_scenarios_does_not_matter_for_membership_counts() -> None:
    sides = split_scenarios(reversed(SCENARIOS), SEED)

    assert Counter(sides.values()) == {Split.TRAIN: 209, Split.VALIDATION: 44, Split.TEST: 44}


def test_a_scenario_that_lost_all_its_messages_takes_no_part_in_the_split() -> None:
    generation = generation_of(SCENARIOS)
    without_one = MessageGeneration(
        generation.message_set,
        generation.scenarios,
        tuple(m for m in generation.messages if m.scenario_id != SCENARIOS[0].id),
        (SCENARIOS[0].id,),
        generation.discards,
    )

    records = split_message_records(without_one, SEED)

    assert SCENARIOS[0].id not in {record.scenario_id for record in records}
    assert len({record.scenario_id for record in records}) == len(SCENARIOS) - 1


def test_the_record_keeps_everything_the_message_had() -> None:
    generation = generation_of(SCENARIOS)

    [record, *_] = split_message_records(generation, SEED)
    message = generation.messages[0]

    assert (record.id, record.scenario_id, record.text, record.intent, record.persona) == (
        message.id,
        message.scenario_id,
        message.text,
        message.intent,
        message.persona,
    )
    assert (record.pii_spans, record.has_injection) == ((), False)


def test_too_few_scenarios_for_an_intent_to_have_three_sides_is_an_error() -> None:
    config = DatagenConfig(messages=MessageConfig(scenarios_per_intent=2))
    scenarios = generate_message_scenarios(config)

    with pytest.raises(ScenarioError) as raised:
        split_scenarios(scenarios, SEED)

    assert raised.value.rule == "splits.too_few_scenarios"
