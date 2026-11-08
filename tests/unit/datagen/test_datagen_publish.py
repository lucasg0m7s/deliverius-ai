"""Publishing: the card made from the manifest, the dry run, the guards and the secret token."""

import inspect
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from huggingface_hub import HfApi
from pydantic import SecretStr
from support.datagen_cli import FakeHub, scripted_harness, wiring_for
from support.llm_runtime import settings_with

from deliverius.cli import texts
from deliverius.cli.datagen import main
from deliverius.cli.wiring import Wiring
from deliverius.datagen import DatasetError
from deliverius.datagen.config import DATAGEN_VERSION
from deliverius.datagen.publish import (
    CARD_FILE,
    HuggingFaceHub,
    PublishError,
    ReviewResult,
    huggingface_hub_for,
    plan_publication,
    publish,
    render_card,
)
from deliverius.datagen.records import Manifest

REPO = "someone/deliverius-synthetic"
TOKEN = "hf_UNIQUE-token-9c41e7d0b2"  # noqa: S105 - a made-up value to look for in outputs
TAG = f"v{DATAGEN_VERSION}"
FAKER_VERSION = "99.1.0"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("published")
    h = scripted_harness(root / "cache.sqlite")
    code = main(
        ["datagen", "all", "--out", str(root / "all"), "--cache", str(root / "cache.sqlite")],
        wiring_for(h),
    )
    assert code == 0
    return root / "all"


def card_of(directory: Path, review: ReviewResult | None = None) -> str:
    return plan_publication(directory, review, FAKER_VERSION).card


def front_matter(card: str) -> dict[str, Any]:
    assert card.startswith("---\n")
    block = card.split("---\n", 2)[1]
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict)
    return loaded


def publishing(arguments: list[str], hub: FakeHub, token: str | None = TOKEN) -> tuple[int, Wiring]:
    settings = settings_with(hf_token=token)
    wiring = wiring_for(settings=settings, hub=hub.factory)
    return main(["datagen", "publish", *arguments], wiring), wiring


def test_the_card_front_matter_has_the_license_the_language_and_one_config_per_set(
    dataset: Path,
) -> None:
    meta = front_matter(card_of(dataset))

    assert meta["license"] == "cc-by-4.0"
    assert meta["language"] == ["pt"]
    assert set(meta["task_categories"]) == {
        "text-classification",
        "token-classification",
        "tabular-classification",
        "text-retrieval",
    }
    configs = {c["config_name"]: c["data_files"] for c in meta["configs"]}
    assert set(configs) == {
        "messages",
        "messages_holdout",
        "fraud_history",
        "policy_qa",
        "e2e_scenarios",
    }
    assert configs["messages"] == [
        {"split": "test", "path": "messages/test.jsonl"},
        {"split": "train", "path": "messages/train.jsonl"},
        {"split": "validation", "path": "messages/validation.jsonl"},
    ]
    assert configs["fraud_history"] == [
        {"split": "test", "path": "fraud_history/test.jsonl"},
        {"split": "train", "path": "fraud_history/train.jsonl"},
    ]
    assert configs["policy_qa"] == [{"split": "test", "path": "policy_qa/test.jsonl"}]


def test_every_path_of_a_config_is_a_file_of_the_dataset(dataset: Path) -> None:
    for config in front_matter(card_of(dataset))["configs"]:
        for entry in config["data_files"]:
            assert (dataset / entry["path"]).is_file()


def test_the_card_has_every_section_the_spec_asks_for(dataset: Path) -> None:
    card = card_of(dataset)

    for heading in (
        "## Synthetic data notice",
        "## How it was generated",
        "## Sizes and splits",
        "## Fields",
        "## Known limitations",
        "## License",
    ):
        assert heading in card
    assert "Everything here is made up" in card
    assert "belong to nobody" in card
    assert "CC BY 4.0" in card


def test_the_card_tells_how_it_was_generated_from_the_manifest(dataset: Path) -> None:
    manifest = Manifest.model_validate_json((dataset / "manifest.json").read_text("utf-8"))
    card = card_of(dataset)

    assert f"Generator version: `{DATAGEN_VERSION}`, seed `42`" in card
    assert f"commit `{'c' * 40} (with uncommitted changes)`" in card
    assert "Faker version: `99.1.0`" in card
    assert "| datagen | `gemini:gemini-3.5-flash-lite` |" in card
    assert "| datagen_holdout | `groq:openai/gpt-oss-120b` |" in card
    for name, digest in manifest.prompt_hashes.items():
        assert f"| {name} | {manifest.prompt_versions[name]} | `{digest}` |" in card
    assert manifest.fraud_test_cutoff is not None
    assert manifest.fraud_test_cutoff.isoformat() in card


def test_the_card_has_the_sizes_and_the_intents(dataset: Path) -> None:
    manifest = Manifest.model_validate_json((dataset / "manifest.json").read_text("utf-8"))
    card = card_of(dataset)

    for name, rows in manifest.counts.items():
        assert f"| {name} | {rows} |" in card
    assert "| item_missing |" in card
    assert "split 70/15/15 by scenario" in card


def test_the_limitations_leave_the_review_and_discard_rates_for_the_report(dataset: Path) -> None:
    card = card_of(dataset)

    assert "Manual review defect rate: not measured yet." in card
    assert "Discard rate of `messages` variations: 0.0% overall (0 discarded" in card
    assert "Discard rate of `messages_holdout` variations: 0.0% overall" in card


def test_the_review_result_goes_into_the_limitations(dataset: Path) -> None:
    card = card_of(dataset, ReviewResult(defect_rate=0.08, sample_size=100))

    assert "Manual review defect rate: 8.0% (sample of 100 messages)." in card
    assert "not measured yet" not in card


def test_discards_of_the_manifest_become_a_rate_with_the_worst_intent(dataset: Path) -> None:
    manifest = Manifest.model_validate_json((dataset / "manifest.json").read_text("utf-8"))
    kept = manifest.intent_counts["messages"]
    manifest = manifest.model_copy(
        update={
            "discards": {
                "messages.too_long.other": kept["other"] // 3,
                "messages.duplicate.sensitive": 1,
            }
        }
    )

    card = render_card(manifest, ReviewResult(), FAKER_VERSION)

    assert "highest for `other`: 25.0%" in card


def test_the_default_faker_version_is_the_installed_one(dataset: Path) -> None:
    card = plan_publication(dataset).card

    assert "Faker version: `" in card
    assert "99.1.0" not in card


def test_a_partial_dataset_only_gets_the_configs_it_has(tmp_path: Path) -> None:
    out = tmp_path / "scenarios"
    assert main(["datagen", "scenarios", "--out", str(out)], wiring_for()) == 0

    meta = front_matter(card_of(out))

    assert [c["config_name"] for c in meta["configs"]] == ["e2e_scenarios"]
    assert "task_categories" not in meta


def test_the_plan_lists_the_files_the_manifest_vouches_for_and_the_manifest(dataset: Path) -> None:
    plan = plan_publication(dataset, faker_version=FAKER_VERSION)

    names = [file.name for file in plan.files]
    assert names[-1] == "manifest.json"
    assert "messages/train.jsonl" in names
    assert all(file.size_bytes > 0 for file in plan.files)
    assert plan.tag == TAG


def test_a_file_changed_after_it_was_written_stops_the_plan(dataset: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    copy.mkdir()
    for path in dataset.rglob("*"):
        if path.is_file():
            target = copy / path.relative_to(dataset)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    (copy / "policy_qa" / "test.jsonl").write_bytes(b"tampered\n")

    with pytest.raises(DatasetError) as error:
        plan_publication(copy)

    assert error.value.file == "policy_qa/test.jsonl"


def test_without_confirmation_it_lists_the_files_and_the_card_and_sends_nothing(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset)], hub, token=None)

    captured = capsys.readouterr()
    assert code == 0
    assert "Nada foi enviado" in captured.out
    assert f"com a tag {TAG}" in captured.out
    assert "  messages/train.jsonl (" in captured.out
    assert "  manifest.json (" in captured.out
    assert "license: cc-by-4.0" in captured.out
    assert hub.tokens == []
    assert (hub.created, hub.uploads, hub.tagged) == ([], [], [])
    [event] = [line for line in captured.err.splitlines() if "datagen.publish_dry_run" in line]
    assert REPO in event


def test_with_confirmation_and_without_the_token_it_fails_citing_the_variable(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub, token=None)

    assert code == 1
    assert "HF_TOKEN" in capsys.readouterr().err
    assert hub.tokens == []
    assert (hub.created, hub.uploads, hub.tagged) == ([], [], [])


def test_an_empty_token_counts_as_missing(dataset: Path) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub, token="  ")  # noqa: S106

    assert code == 1
    assert hub.tokens == []


def test_with_confirmation_it_creates_the_repo_uploads_the_files_and_the_card_and_tags(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub)

    assert code == 0
    assert hub.created == [REPO]
    [(repo, uploaded)] = hub.uploads
    assert repo == REPO
    sent_dataset_files = {name for name in uploaded if name != CARD_FILE}
    assert sent_dataset_files == {
        p.relative_to(dataset).as_posix() for p in dataset.rglob("*") if p.is_file()
    }
    assert all(uploaded[name] == (dataset / name).read_bytes() for name in sent_dataset_files)
    assert uploaded[CARD_FILE].decode("utf-8").startswith("---\nlicense: cc-by-4.0")
    assert hub.tagged == [(REPO, TAG)]
    assert hub.tokens == [TOKEN]
    captured = capsys.readouterr()
    assert f"Publicado em {REPO} com a tag {TAG}" in captured.out
    [event] = [line for line in captured.err.splitlines() if "datagen.published" in line]
    assert TAG in event


def test_a_version_that_is_already_tagged_is_refused_before_the_upload(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub(existing_tags=frozenset({TAG, "v0.1.0"}))

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub)

    assert code == 1
    assert "já foi publicada" in capsys.readouterr().err
    assert (hub.uploads, hub.tagged) == ([], [])


def test_an_untouched_other_tag_does_not_block_the_publication(dataset: Path) -> None:
    hub = FakeHub(existing_tags=frozenset({"v0.0.1"}))

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub)

    assert code == 0
    assert hub.tagged == [(REPO, TAG)]


def test_the_review_options_reach_the_card(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = publishing(
        [
            "--repo",
            REPO,
            "--dir",
            str(dataset),
            "--review-defect-rate",
            "0.07",
            "--review-sample-size",
            "100",
        ],
        FakeHub(),
    )

    assert code == 0
    assert "Manual review defect rate: 7.0% (sample of 100 messages)." in capsys.readouterr().out


@pytest.mark.parametrize("repo", ["", "no-namespace", "a/b/c", "a/ b", "../x/y", "x/y;rm"])
def test_a_repository_that_is_not_user_slash_name_is_refused(
    dataset: Path, repo: str, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", repo, "--dir", str(dataset), "--yes"], hub)

    assert code == 1
    assert "usuario/nome" in capsys.readouterr().err
    assert hub.tokens == []


def test_a_folder_that_is_not_a_dataset_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(tmp_path / "nothing"), "--yes"], hub)

    assert code == 1
    assert "não é confiável ou não existe" in capsys.readouterr().err
    assert hub.tokens == []


def test_the_default_folder_is_the_versioned_one(
    dataset: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    shutil.copytree(dataset, tmp_path / "data" / "generated" / TAG)

    code, _ = publishing(["--repo", REPO], FakeHub())

    assert code == 0
    assert "manifest.json" in capsys.readouterr().out


class SecretLeakingHub(FakeHub):
    def upload_folder(self, repo_id: str, folder: Path, message: str) -> None:
        raise RuntimeError(f"401 for Authorization: Bearer {TOKEN} on {repo_id}")  # noqa: TRY003


def test_a_failure_of_the_hub_reports_only_the_exception_type_never_the_token(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = SecretLeakingHub()

    code, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], hub)

    captured = capsys.readouterr()
    assert code == 1
    assert "RuntimeError" in captured.err
    assert TOKEN not in captured.out + captured.err
    assert hub.tagged == []


def test_the_token_never_appears_in_any_output_of_a_whole_publication(
    dataset: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dry, _ = publishing(["--repo", REPO, "--dir", str(dataset)], FakeHub())
    done, _ = publishing(["--repo", REPO, "--dir", str(dataset), "--yes"], FakeHub())
    refused, _ = publishing(
        ["--repo", REPO, "--dir", str(dataset), "--yes"], FakeHub(existing_tags=frozenset({TAG}))
    )

    captured = capsys.readouterr()
    assert (dry, done, refused) == (0, 0, 1)
    assert TOKEN not in captured.out + captured.err


def test_a_publish_error_never_holds_the_token() -> None:
    hub = FakeHub(fail_with=RuntimeError(TOKEN))
    plan = SimpleNamespace(tag=TAG)

    with pytest.raises(PublishError) as error:
        publish(plan, REPO, SecretStr(TOKEN), hub.factory)  # type: ignore[arg-type]

    assert TOKEN not in str(error.value)
    assert error.value.detail == "RuntimeError"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def create_repo(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_repo", args, kwargs))

    def list_repo_refs(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("list_repo_refs", args, kwargs))
        return SimpleNamespace(tags=[SimpleNamespace(name="v1"), SimpleNamespace(name="v2")])

    def upload_folder(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("upload_folder", args, kwargs))

    def create_tag(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_tag", args, kwargs))


def test_the_adapter_asks_the_hub_for_a_public_dataset_repo_and_its_tags() -> None:
    api = FakeApi()
    hub = HuggingFaceHub(api)  # type: ignore[arg-type]

    hub.create_dataset_repo(REPO)
    tags = hub.tags(REPO)
    hub.upload_folder(REPO, Path("folder"), "message")
    hub.create_tag(REPO, "v9")

    assert tags == frozenset({"v1", "v2"})
    assert api.calls == [
        (
            "create_repo",
            (),
            {"repo_id": REPO, "repo_type": "dataset", "private": False, "exist_ok": True},
        ),
        ("list_repo_refs", (REPO,), {"repo_type": "dataset"}),
        (
            "upload_folder",
            (),
            {
                "repo_id": REPO,
                "repo_type": "dataset",
                "folder_path": Path("folder"),
                "commit_message": "message",
            },
        ),
        ("create_tag", (REPO,), {"tag": "v9", "repo_type": "dataset"}),
    ]


def test_the_arguments_the_adapter_sends_exist_in_the_installed_huggingface_hub() -> None:
    api = FakeApi()
    HuggingFaceHub(api).create_dataset_repo(REPO)  # type: ignore[arg-type]
    HuggingFaceHub(api).tags(REPO)  # type: ignore[arg-type]
    HuggingFaceHub(api).upload_folder(REPO, Path("f"), "m")  # type: ignore[arg-type]
    HuggingFaceHub(api).create_tag(REPO, "v1")  # type: ignore[arg-type]

    for name, args, kwargs in api.calls:
        inspect.signature(getattr(HfApi, name)).bind(None, *args, **kwargs)


def test_the_production_hub_is_built_from_the_secret_token_alone() -> None:
    hub = huggingface_hub_for(SecretStr(TOKEN))

    assert isinstance(hub, HuggingFaceHub)
    assert TOKEN not in repr(hub)


def test_every_publish_error_has_a_text_for_the_operator() -> None:
    for rule in (
        "publish.token_missing",
        "publish.tag_exists",
        "publish.hub_failed",
        "publish.card_name_taken",
        "publish.other",
    ):
        assert texts.publish_error_message(PublishError(rule, "detail"))
