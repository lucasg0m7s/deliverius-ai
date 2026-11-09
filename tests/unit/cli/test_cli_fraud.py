"""`fraud fetch-model`: Portuguese output and the exit codes of `retrieval fetch-model`."""

import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support.llm_runtime import settings_with

from deliverius.artifacts import ArtifactDownloadError, ArtifactFileError
from deliverius.cli.datagen import main
from deliverius.cli.wiring import Wiring
from deliverius.config import Settings
from deliverius.fraud import FraudModelError, fetch_fraud_model


@dataclass
class FakeFetcher:
    downloaded: tuple[str, ...] = ()
    error: Exception | None = None
    folders: list[Path] = field(default_factory=list)

    def __call__(self, model_dir: Path) -> tuple[str, ...]:
        self.folders.append(model_dir)
        if self.error is not None:
            raise self.error
        return self.downloaded


def run(fetcher: FakeFetcher, settings: Settings | None = None) -> tuple[int, str, str]:
    wiring = Wiring(settings=lambda: settings or settings_with(), fetch_fraud_model=fetcher)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["fraud", "fetch-model"], wiring)
    return code, out.getvalue(), err.getvalue()


def test_the_production_wiring_fetches_the_pinned_fraud_model() -> None:
    assert Wiring().fetch_fraud_model is fetch_fraud_model


def test_fetch_model_downloads_into_the_configured_folder(tmp_path: Path) -> None:
    fetcher = FakeFetcher(downloaded=("manifest.json", "logistic.json"))

    code, out, err = run(fetcher, settings_with(fraud_model_dir=tmp_path))

    assert code == 0
    assert fetcher.folders == [tmp_path]
    assert "Modelo de fraude baixado e conferido" in out
    assert "baixado: logistic.json" in out
    assert err == ""


def test_fetch_model_says_when_there_was_nothing_to_download() -> None:
    code, out, _ = run(FakeFetcher())

    assert code == 0
    assert "nada foi baixado" in out


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (ArtifactFileError("model_file.hash_mismatch", "logistic.json"), "SHA-256"),
        (FraudModelError("fraud_model.file_invalid", "manifest.json"), "conteúdo esperado"),
        (FraudModelError("fraud_model.unknown_rule", None), "fraud_model.unknown_rule"),
    ],
)
def test_a_file_that_does_not_match_exits_1_naming_it(error: Exception, fragment: str) -> None:
    code, out, err = run(FakeFetcher(error=error))

    assert code == 1
    assert out == ""
    assert fragment in err
    assert "fraud fetch-model" in err


def test_the_hub_down_exits_2_naming_the_file() -> None:
    code, _, err = run(FakeFetcher(error=ArtifactDownloadError("calibrator.json")))

    assert code == 2
    assert "calibrator.json" in err
    assert "rode de novo" in err
