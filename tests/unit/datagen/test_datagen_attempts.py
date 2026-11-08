"""A repeated run starts each job at the attempt that worked, so it asks the model nothing new."""

import dataclasses
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from langchain_core.exceptions import ModelAPIError
from support.datagen_cli import COMMIT
from support.datagen_configs import CLI_CONFIG
from support.datagen_llm import Answer, DatagenHarness, harness, structured, valid_variations
from support.llm_runtime import settings_with

from deliverius.datagen import generate_message_scenarios, generate_messages
from deliverius.datagen.attempts import FIRST_ATTEMPT, AttemptLog
from deliverius.datagen.discards import DiscardTally
from deliverius.datagen.generation import ModelsForAttempt
from deliverius.datagen.messages import load_message_prompt, render_scenario
from deliverius.datagen.output import read_manifest, write_dataset
from deliverius.datagen.pipeline import Step, llm_access, run_pipeline
from deliverius.datagen.scenarios import MessageSet
from deliverius.llm.cache import SqliteResponseCache
from deliverius.llm.fake import Fail
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import RoleModels

CONFIG = CLI_CONFIG
SCENARIOS = generate_message_scenarios(CONFIG)
HUMANS = {render_scenario(s): s for s in SCENARIOS}
FLAKY = render_scenario(SCENARIOS[0])
MESSAGES = [("system", "prompt"), ("human", "scenario")]
MODEL = "gemini:flash"
NO_DISCARDS = DiscardTally()


def flaky_first_call(cache: Path, text_suffix: str = "") -> DatagenHarness:
    """Every scenario answers; `FLAKY` fails its first call of the run, unless the text differs.

    A different `text_suffix` plays a model that this time answers the first attempt with other
    words: the run that asks it again gets another output.
    """
    calls: Counter[str] = Counter()

    def respond(human: str) -> Answer:
        calls[human] += 1
        if human == FLAKY and calls[human] == 1 and not text_suffix:
            return 0.0, Fail(ModelAPIError("boom"))
        texts = [text + text_suffix for text in valid_variations(HUMANS[human])]
        return structured({"variations": texts})

    return harness(cache, respond)


def read_tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


async def generate_to(out: Path, h: DatagenHarness) -> None:
    access = llm_access(h.runtime, settings_with(), h.cache_path)
    dataset = await run_pipeline("messages", [Step.MESSAGES], CONFIG, COMMIT, access)
    write_dataset(out, dataset.files, dataset.manifest)


def recording(h: DatagenHarness, asked: list[int]) -> ModelsForAttempt:
    models = h.models()

    def for_attempt(attempt: int) -> RoleModels:
        asked.append(attempt)
        return models(attempt)

    return for_attempt


async def test_a_second_run_after_a_failed_attempt_calls_no_model_and_writes_the_same_bytes(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    first = flaky_first_call(cache)
    await generate_to(tmp_path / "one", first)

    second = flaky_first_call(cache, "-another-text")
    await generate_to(tmp_path / "two", second)

    assert first.gemini.tracker.calls == len(SCENARIOS) + 1
    assert second.gemini.tracker.calls == 0
    assert read_tree(tmp_path / "two") == read_tree(tmp_path / "one")


async def test_without_the_log_the_second_run_starts_over_and_asks_the_model_again(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    first = flaky_first_call(cache)
    one = await generate_messages(SCENARIOS, first.models(), CONFIG)

    second = flaky_first_call(cache, "-another-text")
    two = await generate_messages(SCENARIOS, second.models(), CONFIG)

    assert second.gemini.tracker.calls == 1
    assert two != one


async def test_the_attempt_that_worked_is_where_the_next_run_starts(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, MODEL)
    h = flaky_first_call(cache)
    first_asked: list[int] = []
    second_asked: list[int] = []

    await generate_messages(SCENARIOS[:1], recording(h, first_asked), CONFIG, attempt_log=log)
    await generate_messages(SCENARIOS[:1], recording(h, second_asked), CONFIG, attempt_log=log)

    assert first_asked == [1, 2]
    assert second_asked == [2]
    assert h.gemini.tracker.calls == 2


async def test_a_changed_prompt_is_another_job_and_starts_at_the_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, MODEL)
    h = flaky_first_call(cache)
    await generate_messages(SCENARIOS[:1], h.models(), CONFIG, attempt_log=log)
    prompt = load_message_prompt(MessageSet.MAIN)
    edited = dataclasses.replace(prompt, text=prompt.text + " Mais uma regra.", content_hash="new")
    monkeypatch.setattr("deliverius.datagen.messages.load_message_prompt", lambda _: edited)
    asked: list[int] = []

    await generate_messages(SCENARIOS[:1], recording(h, asked), CONFIG, attempt_log=log)

    assert asked == [1]


def test_a_job_with_nothing_recorded_starts_at_the_first_attempt(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite", MODEL)

    assert log.resume(ModelRole.DATAGEN, "key", MESSAGES).attempt == FIRST_ATTEMPT


def test_the_recorded_attempt_survives_a_new_log_on_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "attempts.sqlite"
    AttemptLog(path, MODEL).record(ModelRole.DATAGEN, "key", MESSAGES, 3, NO_DISCARDS)

    assert AttemptLog(path, MODEL).resume(ModelRole.DATAGEN, "key", MESSAGES).attempt == 3


def test_recording_again_replaces_the_attempt(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite", MODEL)
    log.record(ModelRole.DATAGEN, "key", MESSAGES, 3, NO_DISCARDS)
    log.record(ModelRole.DATAGEN, "key", MESSAGES, 2, NO_DISCARDS)

    assert log.resume(ModelRole.DATAGEN, "key", MESSAGES).attempt == 2


@pytest.mark.parametrize(
    ("role", "key", "messages", "model"),
    [
        (ModelRole.DATAGEN_HOLDOUT, "key", MESSAGES, MODEL),
        (ModelRole.DATAGEN, "other-key", MESSAGES, MODEL),
        (ModelRole.DATAGEN, "key", [("system", "prompt edited"), ("human", "scenario")], MODEL),
        (ModelRole.DATAGEN, "key", [("system", "prompt"), ("human", "scenario edited")], MODEL),
        (ModelRole.DATAGEN, "key", [("system", "prompt")], MODEL),
        (ModelRole.DATAGEN, "key", MESSAGES, "gemini:other"),
    ],
    ids=["role", "key", "system-prompt", "human-message", "message-count", "model"],
)
def test_anything_that_changes_the_job_invalidates_the_record(
    tmp_path: Path,
    role: ModelRole,
    key: str,
    messages: list[tuple[str, str]],
    model: str,
) -> None:
    path = tmp_path / "attempts.sqlite"
    AttemptLog(path, MODEL).record(ModelRole.DATAGEN, "key", MESSAGES, 2, NO_DISCARDS)

    assert AttemptLog(path, model).resume(role, key, messages).attempt == FIRST_ATTEMPT


def test_the_file_holds_a_hash_and_a_number_never_a_prompt(tmp_path: Path) -> None:
    path = tmp_path / "attempts.sqlite"
    AttemptLog(path, MODEL).record(ModelRole.DATAGEN, "key", MESSAGES, 2, NO_DISCARDS)

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT job, attempt FROM datagen_attempts").fetchall()

    [(job, attempt)] = rows
    assert attempt == 2
    assert len(job) == 64
    assert "scenario" not in job


def test_the_log_and_the_response_cache_share_a_file_without_touching_each_other(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "shared.sqlite"
    cache = SqliteResponseCache(path, 1)
    AttemptLog(path, MODEL).record(ModelRole.DATAGEN, "key", MESSAGES, 2, NO_DISCARDS)
    cache.clear()

    assert AttemptLog(path, MODEL).resume(ModelRole.DATAGEN, "key", MESSAGES).attempt == 2


async def test_a_record_beyond_the_attempts_allowed_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, MODEL)
    h = flaky_first_call(cache)
    prompt = load_message_prompt(MessageSet.MAIN)
    messages = [("system", prompt.text), ("human", FLAKY)]
    log.record(
        ModelRole.DATAGEN,
        str(SCENARIOS[0].id),
        messages,
        CONFIG.messages.max_attempts + 1,
        NO_DISCARDS,
    )
    asked: list[int] = []

    result = await generate_messages(SCENARIOS[:1], recording(h, asked), CONFIG, attempt_log=log)

    assert result.failed_scenarios == ()
    assert asked == [1, 2]


async def test_a_job_that_never_worked_leaves_no_record(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, MODEL)
    h = harness(cache, lambda _: (0.0, Fail(ModelAPIError("boom"))))

    result = await generate_messages(SCENARIOS[:2], h.models(), CONFIG, attempt_log=log)

    assert len(result.failed_scenarios) == 2
    prompt = load_message_prompt(MessageSet.MAIN)
    for scenario in SCENARIOS[:2]:
        messages = [("system", prompt.text), ("human", render_scenario(scenario))]
        assert log.resume(ModelRole.DATAGEN, str(scenario.id), messages).attempt == FIRST_ATTEMPT


def zero_valid_first(cache: Path, text_suffix: str = "") -> DatagenHarness:
    """`FLAKY` first answers two empty variations (all discarded), then answers well."""
    calls: Counter[str] = Counter()

    def respond(human: str) -> Answer:
        calls[human] += 1
        if human == FLAKY and calls[human] == 1 and not text_suffix:
            return structured({"variations": ["", "   "]})
        texts = [text + text_suffix for text in valid_variations(HUMANS[human])]
        return structured({"variations": texts})

    return harness(cache, respond)


async def test_a_second_run_after_discarded_variations_counts_the_same_and_writes_the_same_bytes(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    first = zero_valid_first(cache)
    await generate_to(tmp_path / "one", first)

    second = zero_valid_first(cache, "-another-text")
    await generate_to(tmp_path / "two", second)

    manifest = read_manifest(tmp_path / "one")
    assert sum(manifest.discards.values()) == 2
    assert first.gemini.tracker.calls == len(SCENARIOS) + 1
    assert second.gemini.tracker.calls == 0
    assert read_tree(tmp_path / "two") == read_tree(tmp_path / "one")


async def test_the_discards_of_the_attempts_before_the_one_that_worked_are_kept_in_the_log(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    log = AttemptLog(cache, MODEL)
    prompt = load_message_prompt(MessageSet.MAIN)
    key, messages = str(SCENARIOS[0].id), [("system", prompt.text), ("human", FLAKY)]
    earlier = DiscardTally()
    earlier.add("empty", SCENARIOS[0].intent.value)
    log.record(ModelRole.DATAGEN, key, messages, 2, earlier)
    calls: Counter[str] = Counter()

    def respond(human: str) -> Answer:
        calls[human] += 1
        if calls[human] == 1:
            return structured({"variations": ["", "  "]})
        return structured({"variations": valid_variations(HUMANS[human])})

    h = harness(cache, respond)

    result = await generate_messages(SCENARIOS[:1], h.models(), CONFIG, attempt_log=log)

    resume = log.resume(ModelRole.DATAGEN, key, messages)
    assert resume.attempt == 3
    assert resume.discards.counts == {("empty", SCENARIOS[0].intent.value): 3}
    assert result.discard_total == 3


def test_a_tally_adds_up_labels_and_survives_a_round_trip_through_json() -> None:
    first, second = DiscardTally(), DiscardTally()
    first.add("empty", "other")
    first.add("empty", "other")
    second.add("too_long", "other")
    second.add("empty", "other")
    first.merge(second)

    restored = DiscardTally.from_json(first.to_json())

    assert first.counts == {("empty", "other"): 3, ("too_long", "other"): 1}
    assert restored.counts == first.counts
    assert restored.to_json() == first.to_json()
