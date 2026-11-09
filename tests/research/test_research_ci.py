"""The CI caches the fraud model and the dataset by their pins and runs the gate after fetching."""

from pathlib import Path

from deliverius.config import Settings
from deliverius.fraud.model_files import REVISION
from research.fraud.config import load_config

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
STEPS = WORKFLOW.read_text(encoding="utf-8")


def test_the_model_cache_key_names_the_pinned_revision() -> None:
    model_dir = Settings(_env_file=None).fraud_model_dir  # type: ignore[call-arg]

    assert f"key: fraud-model-{REVISION[:8]}" in STEPS
    assert f"path: {model_dir.as_posix()}" in STEPS


def test_the_dataset_cache_key_names_the_pinned_dataset_commit() -> None:
    dataset = load_config().dataset

    assert f"key: dataset-v{dataset.version}-{dataset.revision[:8]}" in STEPS
    assert f"path: {dataset.directory.as_posix()}" in STEPS


def test_the_gate_runs_after_the_model_is_fetched_with_the_research_group() -> None:
    assert "uv sync --locked --group research" in STEPS
    assert STEPS.index("fraud fetch-model") < STEPS.index("python -m research.fraud.gate")
