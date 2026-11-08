"""The other-generator test: another model family, another prompt, scenarios the main set lacks."""

from collections.abc import Callable
from pathlib import Path

from support.datagen_configs import MESSAGES_CONFIG
from support.datagen_llm import Answer, DatagenHarness, harness, structured, valid_variations

from deliverius.datagen import (
    MessageScenario,
    MessageSet,
    generate_holdout_messages,
    generate_holdout_scenarios,
    generate_message_scenarios,
    generate_messages,
    holdout_message_records,
)
from deliverius.datagen.generation import PROMPTS_DIRECTORY
from deliverius.datagen.messages import render_scenario
from deliverius.datagen.records import Split
from deliverius.llm.prompts import load_prompt
from deliverius.shared.model_ref import LlmProvider

CONFIG = MESSAGES_CONFIG
MAIN = generate_message_scenarios(CONFIG)


def answering(*batches: tuple[MessageScenario, ...]) -> Callable[[str], Answer]:
    humans = {render_scenario(s): s for batch in batches for s in batch}

    def respond(human: str) -> Answer:
        return structured({"variations": valid_variations(humans[human])})

    return respond


def holdout_harness(tmp_path: Path, holdout: tuple[MessageScenario, ...]) -> DatagenHarness:
    return harness(tmp_path / "cache.sqlite", answering(MAIN), groq=answering(holdout))


async def test_the_holdout_scenarios_are_disjoint_from_the_main_set(tmp_path: Path) -> None:
    holdout = generate_holdout_scenarios(CONFIG, MAIN)
    h = holdout_harness(tmp_path, holdout)

    result = await generate_holdout_messages(CONFIG, h.models(MessageSet.HOLDOUT), MAIN)

    assert result.scenarios == holdout
    assert {s.id for s in result.scenarios}.isdisjoint({s.id for s in MAIN})
    assert {s.signature for s in result.scenarios}.isdisjoint({s.signature for s in MAIN})
    assert len(result.messages) == len(holdout) * CONFIG.messages.variations_per_scenario


async def test_the_holdout_uses_the_other_provider_and_its_own_prompt(tmp_path: Path) -> None:
    holdout = generate_holdout_scenarios(CONFIG, MAIN)
    h = holdout_harness(tmp_path, holdout)

    main_result = await generate_messages(MAIN, h.models(MessageSet.MAIN), CONFIG)
    holdout_result = await generate_holdout_messages(CONFIG, h.models(MessageSet.HOLDOUT), MAIN)

    rows = {(call.role.value, call.provider) for call in h.store.calls}
    assert rows == {("datagen", LlmProvider.GEMINI), ("datagen_holdout", LlmProvider.GROQ)}
    assert h.gemini.tracker.calls == len(MAIN)
    assert h.groq.tracker.calls == len(holdout)
    main_prompt = load_prompt(PROMPTS_DIRECTORY, "messages", 1).text
    holdout_prompt = load_prompt(PROMPTS_DIRECTORY, "holdout", 1).text
    assert {system for system, _ in h.gemini.tracker.prompts} == {main_prompt}
    assert {system for system, _ in h.groq.tracker.prompts} == {holdout_prompt}
    assert main_prompt != holdout_prompt
    assert main_result.message_set is MessageSet.MAIN
    assert holdout_result.message_set is MessageSet.HOLDOUT


async def test_the_holdout_messages_are_all_test_and_have_ids_of_their_own(
    tmp_path: Path,
) -> None:
    holdout = generate_holdout_scenarios(CONFIG, MAIN)
    h = holdout_harness(tmp_path, holdout)
    main_result = await generate_messages(MAIN, h.models(MessageSet.MAIN), CONFIG)
    holdout_result = await generate_holdout_messages(CONFIG, h.models(MessageSet.HOLDOUT), MAIN)

    records = holdout_message_records(holdout_result)

    assert {record.split for record in records} == {Split.TEST}
    assert len(records) == len(holdout_result.messages)
    assert {m.id for m in main_result.messages}.isdisjoint({m.id for m in holdout_result.messages})
