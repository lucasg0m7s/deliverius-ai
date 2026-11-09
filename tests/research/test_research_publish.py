"""Publishing: a dry run by default, never over an existing tag, resumable after a crash."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import SecretStr
from support.research_data import synthetic_table

from deliverius.artifacts import ModelFile, PinnedRepository, sha256_of
from deliverius.datagen.pipeline import CommitInfo
from deliverius.fraud.model_files import FraudManifest, load_fraud_model
from research.fraud import publish
from research.fraud.candidate import candidate_from, write_candidate
from research.fraud.config import load_config
from research.fraud.publish import ModelHub, PublishError, send, stage
from research.fraud.train import research_summary, run_research

CONFIG = load_config().model_copy(update={"bootstrap_resamples": 50})
CLEAN = CommitInfo(commit="a" * 40, dirty=False)


@dataclass
class FakeHub:
    existing_tags: set[str] = field(default_factory=set)
    fail_on_tag: bool = False
    uploads: list[set[str]] = field(default_factory=list)
    tagged: list[tuple[str, str]] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)

    def create_model_repo(self, repo_id: str) -> None:
        self.repos.append(repo_id)

    def tags(self, repo_id: str) -> frozenset[str]:
        return frozenset(self.existing_tags)

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        self.uploads.append({path.name for path in folder.iterdir()})
        return "b" * 40

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None:
        if self.fail_on_tag:
            self.fail_on_tag = False
            raise ConnectionError("dropped")
        self.existing_tags.add(tag)
        self.tagged.append((tag, revision))


@pytest.fixture(scope="module")
def candidate_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    table = synthetic_table()
    result = run_research(table, CONFIG)
    candidate = candidate_from(result.logistic, result.calibrated, CONFIG.review_cost_cents)
    summary = research_summary(result, CONFIG, CLEAN, "c" * 40) | {"mlflow_run_id": "run-1"}
    folder = tmp_path_factory.mktemp("candidate") / "fraud"
    write_candidate(folder, candidate, summary, {})
    return folder


def staged(candidate_dir: Path, tmp_path: Path) -> publish.PublishPlan:
    folder = tmp_path / "staging"
    folder.mkdir()
    return stage(candidate_dir, folder, CONFIG)


def test_the_staged_folder_loads_in_the_application(candidate_dir: Path, tmp_path: Path) -> None:
    plan = staged(candidate_dir, tmp_path)

    manifest = FraudManifest.model_validate_json((plan.folder / "manifest.json").read_bytes())
    pinned = ModelFile("manifest.json", plan.manifest_size, plan.manifest_sha256)
    model = load_fraud_model(plan.folder, PinnedRepository("x/y", "d" * 40), pinned)

    assert {f.name for f in manifest.files} == {
        "logistic.json",
        "calibrator.json",
        "decision.json",
        "README.md",
    }
    assert plan.manifest_sha256 == sha256_of(plan.folder / "manifest.json")
    assert manifest.dataset.revision == CONFIG.dataset.revision
    assert manifest.mlflow_run_id == "run-1"
    assert model.version == CONFIG.model.version
    assert plan.tag == "v1.0.0"


def test_the_card_states_the_synthetic_data_and_the_uses_not_intended(
    candidate_dir: Path, tmp_path: Path
) -> None:
    card = (staged(candidate_dir, tmp_path).folder / "README.md").read_text(encoding="utf-8")

    assert "Synthetic data only" in card
    assert "Not intended" in card
    assert "PR-AUC" in card
    assert "Brier" in card
    assert "no pickle" in card


def test_the_dry_run_sends_nothing(candidate_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub = FakeHub()

    code = publish.main(["--candidate", str(candidate_dir)], hub_for=lambda _: hub)

    out = capsys.readouterr().out
    assert code == 0
    assert hub.repos == hub.uploads == hub.tagged == []
    assert "nada foi enviado" in out
    assert "logistic.json" in out
    assert "G0m7s/deliverius-fraud" in out


def test_confirm_uploads_and_tags_the_commit(
    candidate_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")
    hub = FakeHub()
    tokens: list[SecretStr] = []

    def hub_for(token: SecretStr) -> ModelHub:
        tokens.append(token)
        return hub

    code = publish.main(["--candidate", str(candidate_dir), "--confirm"], hub_for=hub_for)

    assert code == 0
    assert hub.uploads == [
        {"logistic.json", "calibrator.json", "decision.json", "README.md", "manifest.json"}
    ]
    assert hub.tagged == [("v1.0.0", "b" * 40)]
    assert tokens[0].get_secret_value() == "test-token"
    assert "b" * 40 in capsys.readouterr().out


def test_an_existing_tag_is_refused_before_any_upload(candidate_dir: Path, tmp_path: Path) -> None:
    hub = FakeHub(existing_tags={"v1.0.0"})

    with pytest.raises(PublishError) as refused:
        send(staged(candidate_dir, tmp_path), hub)

    assert refused.value.rule == "publish.tag_exists"
    assert hub.uploads == []


def test_a_crash_between_upload_and_tag_is_resumed_by_running_again(
    candidate_dir: Path, tmp_path: Path
) -> None:
    hub = FakeHub(fail_on_tag=True)
    plan = staged(candidate_dir, tmp_path)

    with pytest.raises(ConnectionError):
        send(plan, hub)
    revision = send(plan, hub)

    assert len(hub.uploads) == 2
    assert hub.tagged == [("v1.0.0", revision)]


def test_a_hub_failure_reports_only_the_error_type(
    candidate_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    code = publish.main(
        ["--candidate", str(candidate_dir), "--confirm"],
        hub_for=lambda _: FakeHub(fail_on_tag=True),
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "publish.hub_failed: ConnectionError" in err
    assert "dropped" not in err


def test_without_a_token_nothing_is_sent(
    candidate_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "")
    monkeypatch.chdir(candidate_dir.parent)  # no .env here
    hub = FakeHub()

    code = publish.main(["--candidate", str(candidate_dir), "--confirm"], hub_for=lambda _: hub)

    assert code == 1
    assert "publish.token_missing" in capsys.readouterr().err
    assert hub.uploads == []


def test_a_candidate_trained_on_uncommitted_code_is_refused(
    candidate_dir: Path, tmp_path: Path
) -> None:
    dirty = tmp_path / "dirty"
    research = (candidate_dir / "research.json").read_text(encoding="utf-8")
    dirty.mkdir()
    for path in candidate_dir.iterdir():
        if path.is_file():
            (dirty / path.name).write_bytes(path.read_bytes())
    (dirty / "research.json").write_text(
        research.replace('"code_dirty": false', '"code_dirty": true'), encoding="utf-8"
    )

    with pytest.raises(PublishError) as refused:
        staged(dirty, tmp_path)

    assert refused.value.rule == "publish.dirty_code"
