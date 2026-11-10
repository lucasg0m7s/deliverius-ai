"""Publishing the winner: its files, a manifest the adapter can check, and the refusals."""

import json
from pathlib import Path

import pytest
from support.fake_hub import FakeHub
from support.intent_data import learnable_set
from support.intent_scoring import KeywordScorer, measured

from deliverius.artifacts import sha256_of
from deliverius.datagen.pipeline import CommitInfo
from deliverius.datagen.records import Intent
from research.hub import PublishError
from research.intent import publish
from research.intent.candidate import Approach, CandidateDocument, canonical_json
from research.intent.config import load_config
from research.intent.data import LABELS, Corpus
from research.intent.evaluate import EVALUATION_FILE, evaluate, evaluation_document
from research.intent.publish import IntentManifest, model_files, stage
from research.intent.sample import llm_sample
from research.intent.train import freeze, train_tfidf_approach

CONFIG = load_config()
CLEAN = CommitInfo(commit="a" * 40, dirty=False)
TESTS = {
    Corpus.TEST: learnable_set(Corpus.TEST, 6),
    Corpus.HOLDOUT: learnable_set(Corpus.HOLDOUT, 6),
    Corpus.HUMAN: learnable_set(Corpus.HUMAN, 10),
}


def lineage(dirty: bool = False) -> dict[str, object]:
    return {
        "code_commit": CLEAN.commit,
        "code_dirty": dirty,
        "human_test_sha256": "e" * 64,
        "seed": CONFIG.seed,
        "mlflow_run_id": "eval-run",
    }


def write_evaluation(folder: Path, winner_blind: bool = False, dirty: bool = False) -> None:
    # Blind to everything but `other`: no sensitive message is caught, so nobody is eligible.
    blind = frozenset(Intent) - {Intent.OTHER} if winner_blind else frozenset()
    sample = llm_sample([TESTS[Corpus.TEST], TESTS[Corpus.HOLDOUT]], TESTS[Corpus.HUMAN], 2, 1)
    scored = measured("tfidf", KeywordScorer(blind), 2.0, TESTS)
    evaluation = evaluate([scored], TESTS, sample, CONFIG)
    document = evaluation_document(evaluation, lineage(dirty))
    (folder / EVALUATION_FILE).write_bytes(canonical_json(document))


@pytest.fixture
def candidates(tmp_path: Path) -> Path:
    folder = tmp_path / "candidates"
    trained = train_tfidf_approach(
        learnable_set(Corpus.TRAIN, 12), learnable_set(Corpus.VALIDATION, 4), CONFIG
    )
    freeze(
        Approach.TFIDF,
        trained,
        learnable_set(Corpus.VALIDATION, 4),
        CONFIG,
        commit=CLEAN,
        run_id="train-run",
        root=folder,
    )
    write_evaluation(folder)
    return folder


def staging(tmp_path: Path) -> Path:
    folder = tmp_path / "staging"
    folder.mkdir()
    return folder


def test_the_manifest_lists_every_staged_file_and_the_input_contract(
    candidates: Path, tmp_path: Path
) -> None:
    folder = staging(tmp_path)

    manifest, _ = stage(candidates, folder, CONFIG)

    saved = IntentManifest.model_validate_json((folder / "manifest.json").read_bytes())
    assert saved == manifest
    assert manifest.approach is Approach.TFIDF
    assert manifest.input == "privacy.mask_text"
    assert manifest.labels == LABELS
    assert manifest.human_test_sha256 == "e" * 64
    assert manifest.dataset.revision == CONFIG.dataset.revision
    assert manifest.training_run_id == "train-run"
    assert manifest.evaluation_run_id == "eval-run"
    assert manifest.embedder is None
    assert {f.name for f in manifest.files} == {"README.md", "logistic.json", "tfidf.json"}
    assert all(f.sha256 == sha256_of(folder / f.name) for f in manifest.files)
    assert manifest.metrics["human"]["macro_f1"] == 1.0


def test_the_card_states_the_synthetic_data_the_contract_and_the_rule(
    candidates: Path, tmp_path: Path
) -> None:
    folder = staging(tmp_path)
    stage(candidates, folder, CONFIG)

    card = (folder / "README.md").read_text(encoding="utf-8")

    assert "Synthetic data only" in card
    assert "no person wrote any test" in card
    assert "mask_text" in card
    assert "paired bootstrap" in card
    assert "no pickle" in card
    assert "`sensitive`" in card


def test_the_dry_run_sends_nothing(candidates: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub = FakeHub()

    code = publish.main(["--candidates", str(candidates)], hub_for=lambda _: hub)

    out = capsys.readouterr().out
    assert code == 0
    assert hub.uploads == []
    assert "Vencedor: tfidf" in out
    assert "G0m7s/deliverius-intent" in out
    assert "nada foi enviado" in out


def test_confirm_uploads_the_winner_and_tags_it(
    candidates: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")
    hub = FakeHub()

    code = publish.main(["--candidates", str(candidates), "--confirm"], hub_for=lambda _: hub)

    assert code == 0
    assert hub.uploads == [{"README.md", "logistic.json", "tfidf.json", "manifest.json"}]
    assert hub.tagged == [("v1.0.0", "b" * 40)]


def test_no_winner_publishes_nothing(candidates: Path, tmp_path: Path) -> None:
    write_evaluation(candidates, winner_blind=True)

    with pytest.raises(PublishError) as refused:
        stage(candidates, staging(tmp_path), CONFIG)

    assert refused.value.rule == "publish.no_winner"


def test_an_evaluation_of_uncommitted_code_is_refused(candidates: Path, tmp_path: Path) -> None:
    write_evaluation(candidates, dirty=True)

    with pytest.raises(PublishError) as refused:
        stage(candidates, staging(tmp_path), CONFIG)

    assert refused.value.rule == "publish.dirty_code"


def test_a_candidate_file_changed_after_the_evaluation_is_refused(
    candidates: Path, tmp_path: Path
) -> None:
    (candidates / "tfidf" / "logistic.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PublishError) as refused:
        stage(candidates, staging(tmp_path), CONFIG)

    assert refused.value.rule == "publish.candidate_changed"


def test_bertimbau_also_ships_its_labels_and_calibration(tmp_path: Path) -> None:
    (tmp_path / "model.onnx").write_bytes(b"onnx")
    (tmp_path / "tokenizer.json").write_bytes(b"{}")
    document = CandidateDocument(
        approach=Approach.BERTIMBAU,
        labels=LABELS,
        temperature=1.2,
        threshold=0.7,
        params={},
        seed=1,
        code_commit="a" * 40,
        code_dirty=False,
        dataset_version="1.0.0",
        dataset_revision="d" * 40,
        validation={},
        files={"model.onnx": "x", "tokenizer.json": "y"},
    )

    files = model_files(document, tmp_path)

    assert set(files) == {"model.onnx", "tokenizer.json", "labels.json", "calibration.json"}
    assert json.loads(files["calibration.json"]) == {"temperature": 1.2, "threshold": 0.7}
    assert json.loads(files["labels.json"])["labels"] == list(LABELS)
