"""The generation subcommands: files, manifest, estimate, exit codes and the budget stop."""

from pathlib import Path

import pytest
from support.datagen_cli import COMMIT, scripted_harness, wiring_for
from support.datagen_configs import CLI_CONFIG

from deliverius.cli.datagen import default_output, main
from deliverius.cli.wiring import Wiring, database_runtime
from deliverius.config import Settings
from deliverius.datagen import verify_dataset
from deliverius.datagen.config import DATAGEN_VERSION
from deliverius.datagen.pipeline import CommitInfo

ALL_FILES = {
    "world/customers.jsonl",
    "world/restaurants.jsonl",
    "world/orders.jsonl",
    "fraud_history/train.jsonl",
    "fraud_history/test.jsonl",
    "fraud_ground_truth/all.jsonl",
    "messages/train.jsonl",
    "messages/validation.jsonl",
    "messages/test.jsonl",
    "messages_holdout/test.jsonl",
    "policy_qa/test.jsonl",
    "e2e_scenarios/test.jsonl",
}


def datagen(arguments: list[str], wiring: Wiring) -> int:
    return main(["datagen", *arguments], wiring)


MAIN_SCENARIOS = 11 * CLI_CONFIG.messages.scenarios_per_intent


def test_all_writes_the_canonical_layout_and_one_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = scripted_harness(tmp_path / "cache.sqlite")
    out = tmp_path / "v1"

    code = datagen(
        ["all", "--out", str(out), "--cache", str(tmp_path / "cache.sqlite")], wiring_for(h)
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert f"{len(ALL_FILES)} arquivos" in captured.out
    manifest = verify_dataset(out)
    assert set(manifest.files) == ALL_FILES
    assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == ALL_FILES | {
        "manifest.json"
    }
    assert manifest.datagen_version == DATAGEN_VERSION
    assert (manifest.seed, manifest.commit, manifest.dirty) == (42, COMMIT.commit, True)
    assert manifest.models == {
        "datagen": "gemini:gemini-3.5-flash-lite",
        "datagen_holdout": "groq:openai/gpt-oss-120b",
    }
    assert set(manifest.prompt_versions) == {"messages", "holdout", "rag_pairs"}
    assert set(manifest.prompt_hashes) == set(manifest.prompt_versions)
    assert all(len(digest) == 64 for digest in manifest.prompt_hashes.values())
    assert manifest.fraud_test_cutoff == CLI_CONFIG.fraud_test_cutoff
    assert manifest.label_noise_fraction is not None
    assert manifest.fraud_rate is not None
    counts = manifest.counts
    assert (
        counts["messages"]
        == counts["messages.train"] + counts["messages.validation"] + counts["messages.test"]
    )
    assert counts["messages"] == MAIN_SCENARIOS * CLI_CONFIG.messages.variations_per_scenario
    assert counts["messages_holdout"] == 11 * 2 * CLI_CONFIG.messages.variations_per_scenario
    assert counts["fraud_history.train"] + counts["fraud_history.test"] > 0
    assert sum(manifest.intent_counts["messages.train"].values()) == counts["messages.train"]
    assert set(manifest.intent_counts["messages"]) == {
        "item_missing",
        "wrong_item",
        "quality_issue",
        "late_delivery",
        "not_delivered",
        "billing_issue",
        "cancel_order",
        "coupon_question",
        "policy_question",
        "sensitive",
        "other",
    }
    assert manifest.discards == {}


def test_the_estimate_and_the_use_of_the_day_come_before_the_first_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = scripted_harness(tmp_path / "cache.sqlite")
    seen_before_first_call: list[str] = []
    original = h.gemini.respond

    def spy(human: str):  # type: ignore[no-untyped-def]
        if not seen_before_first_call:
            seen_before_first_call.append(capsys.readouterr().out)
        return original(human)

    h.gemini.respond = spy

    code = datagen(
        ["messages", "--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache.sqlite")],
        wiring_for(h),
    )

    assert code == 0
    [announced] = seen_before_first_call
    assert "Estimativa de chamadas" in announced
    assert f"{MAIN_SCENARIOS} chamadas" in announced
    assert "uso de hoje em gemini: 0 de 400 chamadas; 0 de 2.000.000 tokens" in announced


def read_hashes(directory: Path) -> dict[str, str]:
    return dict(verify_dataset(directory).files)


def test_a_second_run_over_the_same_cache_makes_no_call_and_writes_the_same_files(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    first = scripted_harness(cache)
    assert (
        datagen(["all", "--out", str(tmp_path / "a"), "--cache", str(cache)], wiring_for(first))
        == 0
    )
    second = scripted_harness(cache)

    code = datagen(["all", "--out", str(tmp_path / "b"), "--cache", str(cache)], wiring_for(second))

    assert code == 0
    assert second.gemini.tracker.calls == 0
    assert second.groq.tracker.calls == 0
    assert second.store.calls == []
    assert read_hashes(tmp_path / "a") == read_hashes(tmp_path / "b")
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (
        tmp_path / "b" / "manifest.json"
    ).read_bytes()


def test_another_seed_writes_another_dataset(tmp_path: Path) -> None:
    for seed in ("42", "43"):
        code = datagen(["world", "--seed", seed, "--out", str(tmp_path / seed)], wiring_for())
        assert code == 0

    assert (
        read_hashes(tmp_path / "42")["world/orders.jsonl"]
        != read_hashes(tmp_path / "43")["world/orders.jsonl"]
    )
    assert verify_dataset(tmp_path / "43").seed == 43


@pytest.mark.parametrize(
    ("subcommand", "files", "calls"),
    [
        (
            "world",
            {
                "world/customers.jsonl",
                "world/restaurants.jsonl",
                "world/orders.jsonl",
                "fraud_history/train.jsonl",
                "fraud_history/test.jsonl",
                "fraud_ground_truth/all.jsonl",
            },
            0,
        ),
        ("scenarios", {"e2e_scenarios/test.jsonl"}, 0),
        (
            "messages",
            {"messages/train.jsonl", "messages/validation.jsonl", "messages/test.jsonl"},
            MAIN_SCENARIOS,
        ),
        ("holdout", {"messages_holdout/test.jsonl"}, 22),
        ("rag-pairs", {"policy_qa/test.jsonl"}, 48),
    ],
)
def test_each_step_writes_its_own_files_with_a_manifest_of_its_own(
    tmp_path: Path, subcommand: str, files: set[str], calls: int
) -> None:
    h = scripted_harness(tmp_path / "cache.sqlite")
    out = tmp_path / "partial"

    code = datagen(
        [subcommand, "--out", str(out), "--cache", str(tmp_path / "cache.sqlite")]
        if subcommand in {"messages", "holdout", "rag-pairs"}
        else [subcommand, "--out", str(out)],
        wiring_for(h),
    )

    assert code == 0
    assert set(verify_dataset(out).files) == files
    assert h.gemini.tracker.calls + h.groq.tracker.calls == calls


def test_the_holdout_runs_on_the_other_provider_and_the_manifest_says_which_model(
    tmp_path: Path,
) -> None:
    h = scripted_harness(tmp_path / "cache.sqlite")

    code = datagen(
        ["holdout", "--out", str(tmp_path / "o"), "--cache", str(tmp_path / "cache.sqlite")],
        wiring_for(h),
    )

    assert code == 0
    assert h.gemini.tracker.calls == 0
    assert {call.provider.value for call in h.store.calls} == {"groq"}
    assert verify_dataset(tmp_path / "o").models == {"datagen_holdout": "groq:openai/gpt-oss-120b"}


def test_the_default_folders_are_the_versioned_one_for_all_and_partial_for_the_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert datagen(["scenarios"], wiring_for()) == 0

    assert (tmp_path / "data/generated/partial/scenarios/manifest.json").is_file()
    assert default_output("all") == Path("data/generated") / f"v{DATAGEN_VERSION}"


def test_an_existing_folder_is_refused_before_anything_runs_and_overwrite_replaces_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "existing"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")

    refused = datagen(["world", "--out", str(out)], wiring_for())

    assert refused == 1
    assert "--overwrite" in capsys.readouterr().err
    assert (out / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert datagen(["world", "--out", str(out), "--overwrite"], wiring_for()) == 0
    assert not (out / "keep.txt").exists()
    assert verify_dataset(out).counts["customers"] == CLI_CONFIG.customer_count


def test_the_commit_and_the_dirty_mark_come_from_the_wiring(tmp_path: Path) -> None:
    code = datagen(
        ["scenarios", "--out", str(tmp_path / "o")],
        wiring_for(commit=CommitInfo(commit="uncommitted", dirty=False)),
    )

    assert code == 0
    manifest = verify_dataset(tmp_path / "o")
    assert (manifest.commit, manifest.dirty) == ("uncommitted", False)


def test_the_concurrency_option_limits_the_calls_in_flight(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    h = scripted_harness(cache)

    code = datagen(
        ["messages", "--out", str(tmp_path / "o"), "--cache", str(cache), "--concurrency", "1"],
        wiring_for(h),
    )

    assert code == 0
    assert h.gemini.tracker.max_in_flight == 1


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["datagen"],
        ["datagen", "nonsense"],
        ["datagen", "world", "--seed", "abc"],
        ["datagen", "messages", "--concurrency", "0"],
        ["datagen", "validate-human"],
        ["datagen", "publish"],
        ["datagen", "publish", "--repo", "a/b", "--review-defect-rate", "1.5"],
        ["datagen", "publish", "--repo", "a/b", "--review-sample-size", "0"],
    ],
)
def test_a_usage_error_exits_with_1_never_with_the_budget_code(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments, wiring_for()) == 1
    assert "erro" in capsys.readouterr().err


def test_help_exits_with_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["datagen", "--help"], wiring_for()) == 0
    assert "validate-human" in capsys.readouterr().out


def test_a_setting_that_does_not_parse_is_named_without_its_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiring = Wiring(settings=lambda: Settings(_env_file=None, llm_datagen_model="not-a-model"))  # type: ignore[call-arg]

    assert main(["datagen", "scenarios"], wiring) == 1
    err = capsys.readouterr().err
    assert "LLM_DATAGEN_MODEL" in err
    assert "not-a-model" not in err


def test_a_missing_provider_key_names_the_variable_and_exits_with_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, gemini_api_key="gemini-key-value")  # type: ignore[call-arg]
    wiring = Wiring(settings=lambda: settings, runtime=database_runtime, base_config=CLI_CONFIG)

    code = datagen(
        ["holdout", "--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c.sqlite")], wiring
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "GROQ_API_KEY" in err
    assert "LLM_DATAGEN_HOLDOUT_MODEL" in err
    assert "gemini-key-value" not in err
    assert not (tmp_path / "o").exists()
