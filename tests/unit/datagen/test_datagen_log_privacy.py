"""The events of the design exist; no message, prompt or Faker value reaches a log or a terminal."""

from pathlib import Path

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from support.datagen_cli import COMMIT, FakeHub, answers_for, scripted_harness, wiring_for
from support.datagen_configs import CLI_CONFIG
from support.datagen_llm import Answer, harness, structured
from support.llm_runtime import settings_with

from deliverius.cli.datagen import main
from deliverius.datagen import BudgetExhaustedError, DatagenConfig, generate_message_scenarios
from deliverius.datagen.config import MessageConfig
from deliverius.datagen.messages import load_message_prompt, render_scenario
from deliverius.datagen.output import read_records
from deliverius.datagen.pipeline import Step, llm_access, run_pipeline
from deliverius.datagen.publish import announce_dry_run, plan_publication, publish
from deliverius.datagen.records import CustomerRecord, MessageRecord, Split
from deliverius.datagen.scenarios import MessageSet
from deliverius.llm.fake import Structured

BAD_TEXT = "TEXTO-INVALIDO-7731 [[DESCONHECIDO]]"
SECRET_TOKEN = SecretStr("hf_UNIQUE-log-token-5d1a")
DESIGN_EVENTS = {
    "datagen.run_started",
    "datagen.variation_discarded",
    "datagen.scenario_failed",
    "datagen.budget_exhausted",
    "datagen.run_finished",
    "datagen.publish_dry_run",
    "datagen.published",
}
FOUR_PER_INTENT = DatagenConfig(
    restaurant_count=10,
    messages=MessageConfig(scenarios_per_intent=4, holdout_scenarios_per_intent=1, concurrency=1),
)


@pytest.fixture
def published(tmp_path: Path) -> Path:
    out = tmp_path / "scenarios"
    assert main(["datagen", "scenarios", "--out", str(out)], wiring_for()) == 0
    return out


def forbidden_values(directory: Path) -> set[str]:
    """Everything a log must not carry: what the model wrote, what it was told, Faker's values."""
    batches = [
        read_records(directory, f"messages/{split.value}.jsonl", MessageRecord) for split in Split
    ]
    customers = read_records(directory, "world/customers.jsonl", CustomerRecord)
    values = {message.text for batch in batches for message in batch}
    values |= {
        value
        for customer in customers
        for value in (customer.name, customer.cpf, customer.phone, customer.email, customer.address)
    }
    values |= {load_message_prompt(message_set).text for message_set in MessageSet}
    values |= {render_scenario(s) for s in generate_message_scenarios(CLI_CONFIG)}
    values |= {
        message.text[span.start : span.end]
        for batch in batches
        for message in batch
        for span in message.pii_spans
    }
    return {value for value in values if value}


def test_a_whole_run_leaves_no_message_prompt_or_faker_value_in_any_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache.sqlite"
    out = tmp_path / "all"
    wiring = wiring_for(scripted_harness(cache), settings=settings_with(hf_token="hf_" + "x" * 20))
    assert main(["datagen", "all", "--out", str(out), "--cache", str(cache)], wiring) == 0
    assert main(["datagen", "publish", "--repo", "a/b", "--dir", str(out)], wiring) == 0
    assert main(["datagen", "publish", "--repo", "a/b", "--dir", str(out), "--yes"], wiring) == 0

    captured = capsys.readouterr()
    everything = captured.out + captured.err

    assert '"event": "datagen.run_started"' in captured.err
    assert '"event": "datagen.run_finished"' in captured.err
    assert forbidden_values(out)
    assert [value for value in forbidden_values(out) if value in everything] == []


async def test_every_event_of_the_design_is_emitted_with_counts_identifiers_and_reasons_only(
    tmp_path: Path, published: Path
) -> None:
    config = FOUR_PER_INTENT
    scenarios = generate_message_scenarios(config)
    gemini, _ = answers_for(config)
    failing, partly_bad = render_scenario(scenarios[0]), render_scenario(scenarios[1])

    def respond(human: str) -> Answer:
        if human == failing:
            return structured({"variations": [BAD_TEXT, "", "   "]})
        delay, step = gemini(human)
        if human == partly_bad and isinstance(step, Structured) and isinstance(step.value, dict):
            return structured({"variations": [*step.value["variations"], BAD_TEXT]})
        return delay, step

    cache = tmp_path / "cache.sqlite"
    settings = settings_with()
    h = harness(cache, respond)
    stopped = harness(
        tmp_path / "other.sqlite", respond, settings=settings_with(llm_gemini_daily_requests=3)
    )

    with capture_logs() as logs:
        await run_pipeline(
            "messages", [Step.MESSAGES], config, COMMIT, llm_access(h.runtime, settings, cache)
        )
        with pytest.raises(BudgetExhaustedError) as stop:
            await run_pipeline(
                "messages",
                [Step.MESSAGES],
                config,
                COMMIT,
                llm_access(stopped.runtime, settings, tmp_path / "other.sqlite"),
            )
        plan = plan_publication(published, faker_version="1")
        announce_dry_run(plan, "a/b")
        publish(plan, "a/b", SECRET_TOKEN, FakeHub().factory)

    names = {event["event"] for event in logs}
    assert names >= DESIGN_EVENTS
    by_name = {name: [e for e in logs if e["event"] == name] for name in DESIGN_EVENTS}
    assert {e["reason"] for e in by_name["datagen.variation_discarded"]} >= {
        "unknown_marker",
        "empty",
    }
    for event in by_name["datagen.variation_discarded"]:
        assert set(event) == {"event", "log_level", "scenario_id", "reason"}
    [failed] = by_name["datagen.scenario_failed"]
    assert failed["scenario_id"] == str(scenarios[0].id)
    assert set(failed) == {"event", "log_level", "scenario_id", "attempts", "reason"}
    assert by_name["datagen.run_started"][0]["estimate"] == {"datagen": 44}
    assert by_name["datagen.run_finished"][0]["counts"]["failed_scenarios.messages"] == 1
    [exhausted] = by_name["datagen.budget_exhausted"]
    assert exhausted["remaining"] == stop.value.remaining
    assert 0 < stop.value.remaining < 44

    text = str(logs)
    assert BAD_TEXT not in text
    assert load_message_prompt(MessageSet.MAIN).text not in text
    assert "Mensagem 0 do cenário" not in text
    assert SECRET_TOKEN.get_secret_value() not in text
