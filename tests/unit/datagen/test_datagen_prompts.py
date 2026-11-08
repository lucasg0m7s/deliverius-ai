import pytest

from deliverius.datagen import DatagenConfig, MessageScenario, generate_message_scenarios
from deliverius.datagen.generation import PROMPTS_DIRECTORY
from deliverius.datagen.messages import INTENT_BRIEFS, ROLE_OF, render_scenario
from deliverius.datagen.placeholders import (
    INJECTION_ATTACKS,
    PII_MARKERS,
    Marker,
    PlaceholderFiller,
    markers_in,
)
from deliverius.datagen.records import Intent
from deliverius.datagen.scenarios import MessageSet
from deliverius.llm.prompts import load_prompt
from deliverius.llm.roles import ModelRole
from deliverius.privacy import find_personal_data


@pytest.fixture(scope="module")
def scenarios() -> tuple[MessageScenario, ...]:
    return generate_message_scenarios(DatagenConfig())


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("messages", ModelRole.DATAGEN),
        ("holdout", ModelRole.DATAGEN_HOLDOUT),
        ("rag_pairs", ModelRole.DATAGEN),
    ],
)
def test_each_prompt_is_a_versioned_file_for_its_role(name: str, role: ModelRole) -> None:
    prompt = load_prompt(PROMPTS_DIRECTORY, name, 1)

    assert (prompt.name, prompt.version, prompt.role) == (name, 1, role)
    assert prompt.changed


def test_the_message_prompts_ask_for_the_same_things_in_their_own_words() -> None:
    messages = load_prompt(PROMPTS_DIRECTORY, "messages", 1)
    holdout = load_prompt(PROMPTS_DIRECTORY, "holdout", 1)

    assert messages.text != holdout.text
    assert messages.content_hash != holdout.content_hash
    first_paragraphs = {messages.text.split("\n\n")[0], holdout.text.split("\n\n")[0]}
    assert len(first_paragraphs) == 2
    for prompt in (messages, holdout):
        assert "600" in prompt.text
        assert "[[CPF]]" in prompt.text or "[[NOME]]" in prompt.text
        assert "[[INJECAO]]" in prompt.text
        assert "dado" in prompt.text


def test_the_prompts_never_hold_a_value_that_looks_personal() -> None:
    for name in ("messages", "holdout", "rag_pairs"):
        prompt = load_prompt(PROMPTS_DIRECTORY, name, 1)

        assert find_personal_data(prompt.text) == ()
        assert not any(attack in prompt.text for attack in INJECTION_ATTACKS)


def test_every_message_set_has_a_prompt_and_a_role() -> None:
    assert ROLE_OF == {
        MessageSet.MAIN: ModelRole.DATAGEN,
        MessageSet.HOLDOUT: ModelRole.DATAGEN_HOLDOUT,
    }


def test_every_intent_has_a_brief() -> None:
    assert set(INTENT_BRIEFS) == set(Intent)


def test_the_scenario_block_is_delimited_and_names_the_required_markers() -> None:
    scenario = next(
        s
        for s in generate_message_scenarios(DatagenConfig())
        if Marker.CPF in s.markers and s.facts.items
    )

    block = render_scenario(scenario)

    assert block.startswith("<cenario>\n")
    assert block.endswith("\n</cenario>")
    assert "[[CPF]]" in block
    assert "marcadores_obrigatorios: " in block
    assert INTENT_BRIEFS[scenario.intent] in block
    for item in scenario.facts.items:
        assert item.name in block


def test_a_scenario_without_markers_says_so() -> None:
    scenario = next(s for s in generate_message_scenarios(DatagenConfig()) if not s.markers)

    assert "marcadores_obrigatorios: nenhum" in render_scenario(scenario)


def test_the_block_of_no_scenario_carries_personal_data_or_an_attack(
    scenarios: tuple[MessageScenario, ...],
) -> None:
    for scenario in scenarios:
        block = render_scenario(scenario)

        assert find_personal_data(block) == ()
        assert not any(attack in block for attack in INJECTION_ATTACKS)
        assert set(markers_in(block)) == set(scenario.markers)


def test_nothing_the_filler_writes_was_ever_in_the_block(
    scenarios: tuple[MessageScenario, ...],
) -> None:
    scenario = next(s for s in scenarios if len(s.markers) >= 2 and s.facts.items)
    block = render_scenario(scenario)
    template = " ".join(f"[[{marker.value}]]" for marker in (*PII_MARKERS, Marker.INJECTION))

    filled = PlaceholderFiller(42).fill(template, scenario_id=scenario.id, variation=0)

    for span in filled.pii_spans:
        assert filled.text[span.start : span.end] not in block
    assert not any(attack in block for attack in INJECTION_ATTACKS)
