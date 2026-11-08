from collections import Counter
from random import Random

import pytest

from deliverius.datagen import (
    DatagenConfig,
    MessageScenario,
    MessageSet,
    generate_holdout_scenarios,
    generate_message_scenarios,
)
from deliverius.datagen.config import MessageConfig
from deliverius.datagen.placeholders import PII_MARKERS, Marker
from deliverius.datagen.records import Intent
from deliverius.datagen.rng import IdFactory, Layer
from deliverius.datagen.scenarios import (
    ScenarioError,
    Tone,
    _draw_scenario_id,
    facts_signature,
)
from deliverius.orders import OrderStatus

NO_ORDER = {Intent.COUPON_QUESTION, Intent.POLICY_QUESTION, Intent.OTHER}


@pytest.fixture(scope="module")
def main() -> tuple[MessageScenario, ...]:
    return generate_message_scenarios(DatagenConfig())


@pytest.fixture(scope="module")
def holdout(main: tuple[MessageScenario, ...]) -> tuple[MessageScenario, ...]:
    return generate_holdout_scenarios(DatagenConfig(), main)


def pii_of(scenario: MessageScenario) -> list[Marker]:
    return [marker for marker in scenario.markers if marker in PII_MARKERS]


def test_the_main_set_has_exactly_the_configured_scenarios_for_each_of_the_eleven_intents(
    main: tuple[MessageScenario, ...],
) -> None:
    counts = Counter(scenario.intent for scenario in main)

    assert set(counts) == set(Intent)
    assert set(counts.values()) == {27}
    assert len(main) == 297


def test_about_thirty_percent_ask_for_one_or_two_personal_data_markers(
    main: tuple[MessageScenario, ...],
) -> None:
    with_pii = [scenario for scenario in main if pii_of(scenario)]

    assert len(with_pii) == 89
    assert {len(pii_of(scenario)) for scenario in with_pii} == {1, 2}
    assert all(len(set(scenario.markers)) == len(scenario.markers) for scenario in main)
    assert all(
        list(scenario.markers) == sorted(scenario.markers, key=list(Marker).index)
        for scenario in main
    )


def test_about_five_percent_ask_for_an_injection_and_never_in_a_sensitive_scenario(
    main: tuple[MessageScenario, ...],
) -> None:
    injected = [scenario for scenario in main if Marker.INJECTION in scenario.markers]

    assert len(injected) == 15
    assert Intent.SENSITIVE not in {scenario.intent for scenario in injected}


def test_the_facts_fit_the_intent(main: tuple[MessageScenario, ...]) -> None:
    for scenario in main:
        facts = scenario.facts
        if scenario.intent in NO_ORDER:
            assert (facts.items, facts.total_cents, facts.status) == ((), None, None)
            assert facts.topic is not None
            continue
        assert facts.items
        assert facts.total_cents is not None
        assert facts.total_cents >= sum(i.unit_price_cents * i.quantity for i in facts.items)
        assert facts.payment_method is not None
        assert (facts.delay_minutes is not None) == (scenario.intent is Intent.LATE_DELIVERY)
        if facts.affected_item is not None:
            assert facts.affected_item in {item.name for item in facts.items}
        if scenario.intent is Intent.CANCEL_ORDER:
            assert facts.status in {
                OrderStatus.PLACED,
                OrderStatus.ACCEPTED,
                OrderStatus.PREPARING,
            }
        if scenario.intent is Intent.SENSITIVE:
            assert facts.topic is not None


def test_a_missing_wrong_or_poor_quality_item_names_the_item_of_the_problem(
    main: tuple[MessageScenario, ...],
) -> None:
    affected = {Intent.ITEM_MISSING, Intent.WRONG_ITEM, Intent.QUALITY_ISSUE}

    assert all(
        (scenario.facts.affected_item is not None) == (scenario.intent in affected)
        for scenario in main
    )


def test_the_personas_vary_on_every_axis(main: tuple[MessageScenario, ...]) -> None:
    personas = [scenario.persona for scenario in main]

    assert {persona.tone for persona in personas} == set(Tone)
    assert {persona.slang for persona in personas} == {True, False}
    assert {persona.typos for persona in personas} == {True, False}
    assert {persona.vague for persona in personas} == {True, False}
    assert len({persona.formality for persona in personas}) == 3
    assert len({persona.length for persona in personas}) == 3


def test_the_persona_label_is_stable_text(main: tuple[MessageScenario, ...]) -> None:
    label = main[0].persona.label

    assert label == main[0].persona.label
    assert label.count(";") == 5
    assert label.startswith("tone=")


def test_identifiers_and_fact_signatures_are_unique_in_the_main_set(
    main: tuple[MessageScenario, ...],
) -> None:
    assert len({scenario.id for scenario in main}) == len(main)
    assert len({scenario.signature for scenario in main}) == len(main)


def test_the_signature_ignores_the_persona_and_the_identifier_but_not_the_intent(
    main: tuple[MessageScenario, ...],
) -> None:
    scenario = main[0]
    other_intent = Intent.OTHER if scenario.intent is not Intent.OTHER else Intent.WRONG_ITEM

    assert scenario.signature == facts_signature(scenario.intent, scenario.facts)
    assert scenario.signature != facts_signature(other_intent, scenario.facts)


def test_the_same_configuration_gives_the_same_scenarios(
    main: tuple[MessageScenario, ...],
) -> None:
    assert generate_message_scenarios(DatagenConfig()) == main


def test_another_seed_gives_other_scenarios(main: tuple[MessageScenario, ...]) -> None:
    other = generate_message_scenarios(DatagenConfig(seed=43))

    assert other != main
    assert {s.id for s in other}.isdisjoint({s.id for s in main})
    order_intents = {s.intent for s in main} - NO_ORDER
    assert {s.signature for s in other if s.intent in order_intents}.isdisjoint(
        {s.signature for s in main}
    )


def test_the_other_generator_set_shares_no_identifier_and_no_facts_signature(
    main: tuple[MessageScenario, ...], holdout: tuple[MessageScenario, ...]
) -> None:
    assert len(holdout) == 55
    assert set(Counter(scenario.intent for scenario in holdout).values()) == {5}
    assert {s.id for s in holdout}.isdisjoint({s.id for s in main})
    assert {s.signature for s in holdout}.isdisjoint({s.signature for s in main})
    assert len({s.signature for s in holdout}) == len(holdout)


def test_the_other_generator_set_comes_from_its_own_random_stream(
    main: tuple[MessageScenario, ...], holdout: tuple[MessageScenario, ...]
) -> None:
    assert MessageSet.MAIN.scenario_layer is Layer.SCENARIOS
    assert MessageSet.HOLDOUT.scenario_layer is Layer.HOLDOUT_SCENARIOS
    assert generate_holdout_scenarios(DatagenConfig(), main) == holdout
    assert [s.persona for s in holdout[:10]] != [s.persona for s in main[:10]]


def test_a_signature_already_taken_is_drawn_again(
    holdout: tuple[MessageScenario, ...],
) -> None:
    config = DatagenConfig()

    redrawn = generate_holdout_scenarios(config, holdout)

    assert {s.signature for s in redrawn}.isdisjoint({s.signature for s in holdout})
    assert {s.id for s in redrawn}.isdisjoint({s.id for s in holdout})
    assert len(redrawn) == len(holdout)


class RepeatingRandom(Random):
    """Hands out the same bits twice, so the first identifier drawn collides."""

    def __init__(self, values: list[int]) -> None:
        super().__init__(0)
        self._values = values

    def getrandbits(self, k: int) -> int:
        return self._values.pop(0)


def test_an_identifier_already_used_is_drawn_again() -> None:
    config = DatagenConfig()
    taken = IdFactory(RepeatingRandom([5])).next_id(config.reference_instant)
    used = {taken}

    identifier = _draw_scenario_id(IdFactory(RepeatingRandom([5, 5, 6])), config, used)

    assert identifier != taken
    assert used == {taken, identifier}


def test_a_topic_pool_that_cannot_give_enough_distinct_facts_is_an_error() -> None:
    config = DatagenConfig(messages=MessageConfig(scenarios_per_intent=60))

    with pytest.raises(ScenarioError) as raised:
        generate_message_scenarios(config)

    assert raised.value.rule == "scenarios.facts_exhausted"
    assert raised.value.intent is Intent.COUPON_QUESTION


def test_the_sensitive_topics_are_reused_when_the_order_keeps_the_facts_apart(
    main: tuple[MessageScenario, ...],
) -> None:
    topics = [s.facts.topic for s in main if s.intent is Intent.SENSITIVE]

    assert len(topics) == 27
    assert len(set(topics)) == 8
