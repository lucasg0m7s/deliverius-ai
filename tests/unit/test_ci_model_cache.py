"""The CI keys the model cache on the pinned revision and fetches it before integration tests."""

from pathlib import Path

from deliverius.config import Settings
from deliverius.retrieval import MODEL_REVISION

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_the_cache_key_names_the_pinned_revision() -> None:
    assert f"key: e5-small-{MODEL_REVISION[:8]}" in WORKFLOW.read_text(encoding="utf-8")


def test_the_cache_folder_is_where_the_application_looks_for_the_model() -> None:
    model_dir = Settings(_env_file=None).retrieval_model_dir  # type: ignore[call-arg]

    assert f"path: {model_dir.as_posix()}" in WORKFLOW.read_text(encoding="utf-8")


def test_the_model_is_fetched_before_the_integration_tests() -> None:
    steps = WORKFLOW.read_text(encoding="utf-8")

    fetch = steps.index("retrieval fetch-model")
    integration = steps.index("pytest -m integration")

    assert fetch < integration
