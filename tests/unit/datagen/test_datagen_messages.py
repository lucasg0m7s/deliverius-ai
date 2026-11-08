from collections import Counter
from pathlib import Path

import pytest
from langchain_core.exceptions import ModelAPIError
from structlog.testing import capture_logs
from support.datagen_configs import MESSAGES_CONFIG
from support.datagen_llm import Answer, DatagenHarness, harness, structured, valid_variations

from deliverius.datagen import (
    DiscardReason,
    MessageGeneration,
    MessageScenario,
    MessageSet,
    generate_message_scenarios,
    generate_messages,
)
from deliverius.datagen.generation import PROMPTS_DIRECTORY
from deliverius.datagen.messages import check_variation, render_scenario
from deliverius.datagen.placeholders import Marker
from deliverius.datagen.records import Intent, PiiKind
from deliverius.llm.fake import Fail
from deliverius.llm.prompts import load_prompt
from deliverius.privacy import find_personal_data
from deliverius.privacy.documents import is_valid_cpf
from deliverius.shared import InvariantError
from deliverius.shared.model_ref import LlmProvider

CONFIG = MESSAGES_CONFIG
SCENARIOS = generate_message_scenarios(CONFIG)
PER_SCENARIO = CONFIG.messages.variations_per_scenario


def variations(texts: list[str]) -> Answer:
    return structured({"variations": texts})


def answers_for(
    scenarios: tuple[MessageScenario, ...], override: dict[MessageScenario, Answer] | None = None
) -> dict[str, Answer]:
    answers = {render_scenario(s): variations(valid_variations(s)) for s in scenarios}
    for scenario, answer in (override or {}).items():
        answers[render_scenario(scenario)] = answer
    return answers


def with_markers() -> MessageScenario:
    return next(s for s in SCENARIOS if s.markers)


def without_markers() -> list[MessageScenario]:
    return [s for s in SCENARIOS if not s.markers]


def run_harness(tmp_path: Path, answers: dict[str, Answer]) -> DatagenHarness:
    return harness(tmp_path / "cache.sqlite", lambda human: answers[human])


async def generate(
    tmp_path: Path, answers: dict[str, Answer] | None = None
) -> tuple[MessageGeneration, DatagenHarness]:
    h = run_harness(tmp_path, answers or answers_for(SCENARIOS))
    return await generate_messages(SCENARIOS, h.models(), CONFIG), h


async def test_every_scenario_gets_its_variations_in_the_order_of_the_scenarios(
    tmp_path: Path,
) -> None:
    result, h = await generate(tmp_path)

    assert len(result.messages) == len(SCENARIOS) * PER_SCENARIO
    assert [m.scenario_id for m in result.messages] == [
        s.id for s in SCENARIOS for _ in range(PER_SCENARIO)
    ]
    assert result.discard_total == 0
    assert result.failed_scenarios == ()
    assert {m.intent for m in result.messages} == set(Intent)
    assert len({m.id for m in result.messages}) == len(result.messages)
    assert {m.id.version for m in result.messages} == {7}
    assert h.gemini.tracker.calls == len(SCENARIOS)
    assert h.providers_used() == {LlmProvider.GEMINI}


async def test_the_intent_is_the_scenarios_even_if_the_text_is_about_something_else(
    tmp_path: Path,
) -> None:
    scenario = next(s for s in without_markers() if s.intent is not Intent.LATE_DELIVERY)
    about_delay = [f"Meu pedido está atrasado há {n} minutos, quase uma hora" for n in range(10)]
    answers = answers_for(SCENARIOS, {scenario: variations(about_delay)})

    result, _ = await generate(tmp_path, answers)

    written = [m for m in result.messages if m.scenario_id == scenario.id]
    assert [m.text for m in written] == about_delay
    assert {m.intent for m in written} == {scenario.intent}


async def test_a_personal_data_marker_becomes_a_valid_fake_value_with_its_position(
    tmp_path: Path,
) -> None:
    scenario = next(s for s in SCENARIOS if Marker.CPF in s.markers)

    result, _ = await generate(tmp_path)

    written = [m for m in result.messages if m.scenario_id == scenario.id]
    assert len(written) == PER_SCENARIO
    for message in written:
        assert "[[" not in message.text
        [cpf] = [span for span in message.pii_spans if span.kind is PiiKind.CPF]
        digits = "".join(c for c in message.text[cpf.start : cpf.end] if c.isdigit())
        assert is_valid_cpf(digits)
    assert len({m.text for m in written}) == PER_SCENARIO


async def test_an_injection_marker_marks_the_message_and_only_those(tmp_path: Path) -> None:
    result, _ = await generate(tmp_path)

    injected = {s.id for s in SCENARIOS if Marker.INJECTION in s.markers}
    assert injected
    assert {m.scenario_id for m in result.messages if m.has_injection} == injected


async def test_the_persona_label_of_the_scenario_is_written_on_each_message(
    tmp_path: Path,
) -> None:
    result, _ = await generate(tmp_path)

    by_scenario = {s.id: s.persona.label for s in SCENARIOS}
    assert all(m.persona == by_scenario[m.scenario_id] for m in result.messages)


async def test_no_prompt_carries_personal_data_or_a_value_the_code_will_write(
    tmp_path: Path,
) -> None:
    result, h = await generate(tmp_path)

    prompt = load_prompt(PROMPTS_DIRECTORY, "messages", 1)
    for system, human in h.gemini.tracker.prompts:
        assert system == prompt.text
        assert find_personal_data(system + human) == ()
    sent = " ".join(human for _, human in h.gemini.tracker.prompts)
    values = [m.text[s.start : s.end] for m in result.messages for s in m.pii_spans]
    assert values
    assert not any(value in sent for value in values)


async def test_each_kind_of_invalid_variation_is_discarded_counted_and_logged_without_text(
    tmp_path: Path,
) -> None:
    scenario = with_markers()
    a, b, c, d = valid_variations(scenario, 4)
    unrequested = next(m for m in Marker if m not in scenario.markers)
    texts = [
        a,
        "   ",
        "x" * 601,
        f"{a} [[CNPJ]]",
        f"{a} [[{unrequested.value}]]",
        "texto sem nenhum marcador SEGREDO-NO-LOG",
        a,
        b,
        c,
        d,
        "sobra um",
        "sobra dois",
    ]
    answers = answers_for(SCENARIOS, {scenario: variations(texts)})

    with capture_logs() as logs:
        result, _ = await generate(tmp_path, answers)

    written = [m.text for m in result.messages if m.scenario_id == scenario.id]
    assert len(written) == 4
    assert result.discards == {
        (DiscardReason.EMPTY, scenario.intent): 1,
        (DiscardReason.TOO_LONG, scenario.intent): 1,
        (DiscardReason.UNKNOWN_MARKER, scenario.intent): 1,
        (DiscardReason.UNREQUESTED_MARKER, scenario.intent): 1,
        (DiscardReason.MISSING_MARKER, scenario.intent): 1,
        (DiscardReason.DUPLICATE, scenario.intent): 1,
        (DiscardReason.SURPLUS, scenario.intent): 2,
    }
    assert result.discard_total == 8
    assert result.discards_by_intent() == {scenario.intent: 8}
    events = [e for e in logs if e["event"] == "datagen.variation_discarded"]
    assert Counter(e["reason"] for e in events) == Counter(
        {
            "empty": 1,
            "too_long": 1,
            "unknown_marker": 1,
            "unrequested_marker": 1,
            "missing_marker": 1,
            "duplicate": 1,
            "surplus": 2,
        }
    )
    assert {e["scenario_id"] for e in events} == {str(scenario.id)}
    assert "SEGREDO-NO-LOG" not in str(logs)
    assert "xxxxxxxx" not in str(logs)


async def test_a_repeat_across_scenarios_is_dropped_from_the_later_one_after_normalizing(
    tmp_path: Path,
) -> None:
    first, second = without_markers()[:2]
    answers = answers_for(
        SCENARIOS,
        {
            first: variations(["Oi, meu pedido não chegou", f"outro texto {first.id}"]),
            second: variations(["Oi,  meu pedido   não chegou", f"outro texto {second.id}"]),
        },
    )

    result, _ = await generate(tmp_path, answers)

    assert next(m.text for m in result.messages if m.scenario_id == first.id) == (
        "Oi, meu pedido não chegou"
    )
    assert len([m for m in result.messages if m.scenario_id == second.id]) == 1
    assert result.discards == {(DiscardReason.DUPLICATE, second.intent): 1}


async def test_a_scenario_with_nothing_valid_is_tried_again_with_a_new_cache_key(
    tmp_path: Path,
) -> None:
    scenario = SCENARIOS[5]
    human = render_scenario(scenario)
    calls: Counter[str] = Counter()

    def respond(prompt: str) -> Answer:
        calls[prompt] += 1
        if prompt == human and calls[prompt] == 1:
            return variations(["", "  "])
        return variations(
            valid_variations(next(s for s in SCENARIOS if render_scenario(s) == prompt))
        )

    h = harness(tmp_path / "cache.sqlite", respond)

    result = await generate_messages(SCENARIOS, h.models(), CONFIG)

    assert calls[human] == 2
    assert len([m for m in result.messages if m.scenario_id == scenario.id]) == PER_SCENARIO
    assert result.failed_scenarios == ()
    assert result.discards == {(DiscardReason.EMPTY, scenario.intent): 2}
    assert h.gemini.tracker.calls == len(SCENARIOS) + 1


async def test_a_provider_error_is_tried_again_like_an_invalid_answer(tmp_path: Path) -> None:
    scenario = SCENARIOS[0]
    human = render_scenario(scenario)
    calls: Counter[str] = Counter()
    answers = answers_for(SCENARIOS)

    def respond(prompt: str) -> Answer:
        calls[prompt] += 1
        if prompt == human and calls[prompt] == 1:
            return 0.0, Fail(ModelAPIError("boom"))
        return answers[prompt]

    h = harness(tmp_path / "cache.sqlite", respond)

    result = await generate_messages(SCENARIOS, h.models(), CONFIG)

    assert calls[human] == 2
    assert len(result.messages) == len(SCENARIOS) * PER_SCENARIO
    assert result.discard_total == 0


async def test_a_scenario_that_fails_every_attempt_is_logged_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    scenario = SCENARIOS[7]
    answers = answers_for(SCENARIOS, {scenario: variations([""])})

    with capture_logs() as logs:
        result, h = await generate(tmp_path, answers)

    assert result.failed_scenarios == (scenario.id,)
    assert scenario.id not in {m.scenario_id for m in result.messages}
    assert len(result.messages) == (len(SCENARIOS) - 1) * PER_SCENARIO
    assert h.gemini.tracker.calls == len(SCENARIOS) - 1 + CONFIG.messages.max_attempts
    [failure] = [e for e in logs if e["event"] == "datagen.scenario_failed"]
    assert failure["scenario_id"] == str(scenario.id)
    assert failure["attempts"] == CONFIG.messages.max_attempts
    assert failure["reason"] == "no_valid_output"


async def test_the_result_does_not_depend_on_the_order_the_answers_arrive_in(
    tmp_path: Path,
) -> None:
    answers = answers_for(SCENARIOS)
    ranks = {render_scenario(s): index for index, s in enumerate(SCENARIOS)}
    last = len(SCENARIOS)

    h_reversed = harness(
        tmp_path / "reversed.sqlite",
        lambda human: (0.002 * (last - ranks[human]), answers[human][1]),
    )
    reversed_result = await generate_messages(SCENARIOS, h_reversed.models(), CONFIG)
    h_plain = harness(tmp_path / "plain.sqlite", lambda human: answers[human])
    plain_result = await generate_messages(SCENARIOS, h_plain.models(), CONFIG)

    assert reversed_result == plain_result


async def test_the_calls_run_in_parallel_but_never_more_than_the_limit(tmp_path: Path) -> None:
    answers = answers_for(SCENARIOS)
    h = harness(tmp_path / "cache.sqlite", lambda human: (0.01, answers[human][1]))

    await generate_messages(SCENARIOS, h.models(), CONFIG)

    assert 1 < h.gemini.tracker.max_in_flight <= CONFIG.messages.concurrency


async def test_a_model_of_another_role_is_refused(tmp_path: Path) -> None:
    h = run_harness(tmp_path, answers_for(SCENARIOS))

    with pytest.raises(InvariantError, match=r"generation\.role_mismatch"):
        await generate_messages(SCENARIOS, h.models(MessageSet.HOLDOUT), CONFIG)


async def test_the_same_inputs_write_the_same_messages_and_ids(tmp_path: Path) -> None:
    first, _ = await generate(tmp_path / "one")
    second, _ = await generate(tmp_path / "two")

    assert first == second


@pytest.mark.parametrize(
    ("text", "required", "expected"),
    [
        ("ok", [], None),
        ("", [], DiscardReason.EMPTY),
        ("   \n ", [], DiscardReason.EMPTY),
        ("a" * 600, [], None),
        ("a" * 601, [], DiscardReason.TOO_LONG),
        ("[[CPF]]", [Marker.CPF], None),
        ("[[CPF]] [[CPF]]", [Marker.CPF], None),
        ("[[XYZ]] [[CPF]]", [Marker.CPF], DiscardReason.UNKNOWN_MARKER),
        ("[[CPF]] [[NOME]]", [Marker.CPF], DiscardReason.UNREQUESTED_MARKER),
        ("sem marcador", [Marker.CPF], DiscardReason.MISSING_MARKER),
    ],
)
def test_the_rules_of_a_valid_variation(
    text: str, required: list[Marker], expected: DiscardReason | None
) -> None:
    assert check_variation(text, required, 600) is expected


def test_the_length_is_checked_before_any_pattern_runs() -> None:
    hostile = "[[" * 301

    assert check_variation(hostile, [], 600) is DiscardReason.TOO_LONG
