"""MLflow for the intent study: the local store the fraud study uses, its own experiment."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import mlflow

TRACKING_DIR: Final = Path("mlruns")
EXPERIMENT: Final = "deliverius-intent"


def use_experiment(tracking_dir: Path = TRACKING_DIR) -> None:
    tracking_dir.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{tracking_dir.as_posix()}/mlflow.db")
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, (tracking_dir / "artifacts").resolve().as_uri())
    mlflow.set_experiment(EXPERIMENT)


def numeric_metrics(prefix: str, values: Mapping[str, Any]) -> dict[str, float]:
    """The numbers of `values` as `<prefix>_<name>`; `None` and text are left out."""
    return {
        f"{prefix}_{name}": float(value)
        for name, value in values.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
