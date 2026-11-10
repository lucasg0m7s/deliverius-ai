"""Publishes the winner of the evaluation to the Hugging Face Hub, with its manifest and card.

`python -m research.intent.publish` is a dry run: it stages the winner's files (design D10), the
English card and `manifest.json`, shows them and sends nothing. `--confirm` sends them through
`research.hub` (no overwrite of a tag, resumable). It refuses when the evaluation has no winner
(the decision goes back to the author), when code or candidate came from uncommitted changes, and
when a candidate file changed since it was frozen.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict

from deliverius.artifacts import sha256_of
from deliverius.cli.console import use_utf8_output
from deliverius.retrieval.model_files import MODEL_REPOSITORY, MODEL_REVISION
from research.hub import HubFactory, PublishError, Staged, Upload, huggingface_model_hub, publish
from research.intent.candidate import (
    CANDIDATES_DIR,
    Approach,
    CandidateDocument,
    CandidateError,
    candidate_dir,
    canonical_json,
    read_candidate,
)
from research.intent.config import IntentConfig, load_config
from research.intent.evaluate import EVALUATION_FILE, SAMPLE

logger = structlog.get_logger(__name__)

MANIFEST_FILE: Final = "manifest.json"
CARD_FILE: Final = "README.md"
LABELS_FILE: Final = "labels.json"
CALIBRATION_FILE: Final = "calibration.json"
INPUT_CONTRACT: Final = "privacy.mask_text"
SETS: Final = ("test", "holdout", "human", SAMPLE)


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ManifestFile(_Document):
    name: str
    size_bytes: int
    sha256: str


class DatasetLineage(_Document):
    repository: str
    version: str
    revision: str


class IntentManifest(_Document):
    """What the serving adapter checks before it loads anything (change 9)."""

    model_version: str
    approach: Approach
    input: str
    labels: tuple[str, ...]
    temperature: float | None
    threshold: float | None
    dataset: DatasetLineage
    human_test_sha256: str
    embedder: dict[str, str] | None
    code_commit: str
    training_run_id: str | None
    evaluation_run_id: str | None
    metrics: dict[str, dict[str, float | None]]
    latency_ms: dict[str, float]
    files: tuple[ManifestFile, ...]


def winner_of(evaluation: dict[str, Any]) -> Approach:
    winner = evaluation["choice"]["winner"]
    if winner is None:
        raise PublishError("publish.no_winner", "nenhuma abordagem elegível pela regra")
    if winner == Approach.LLM.value:
        raise PublishError("publish.nothing_to_publish", "the LLM baseline has no model files")
    return Approach(winner)


def _require_clean(document: CandidateDocument, evaluation: dict[str, Any]) -> None:
    if document.code_dirty or evaluation["lineage"]["code_dirty"]:
        raise PublishError("publish.dirty_code", document.code_commit)


def model_files(document: CandidateDocument, folder: Path) -> dict[str, bytes]:
    """The winner's files (design D10); BERTimbau also gets its labels and calibration apart."""
    files = {name: (folder / name).read_bytes() for name in sorted(document.files)}
    if document.approach is Approach.BERTIMBAU:
        files[LABELS_FILE] = canonical_json({"labels": list(document.labels)})
        files[CALIBRATION_FILE] = canonical_json(
            {"temperature": document.temperature, "threshold": document.threshold}
        )
    return files


def manifest_metrics(approach: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    metrics: dict[str, dict[str, float | None]] = {}
    for name in SETS:
        found = approach["sets"].get(name)
        if found is not None:
            metrics[name] = {
                "macro_f1": found["macro_f1"]["point"],
                "macro_f1_low": found["macro_f1"]["low"],
                "macro_f1_high": found["macro_f1"]["high"],
                "sensitive_recall": found["sensitive_recall"],
                "ece": found["ece"],
                "coverage_at_threshold": found["coverage_at_threshold"],
                "error_at_threshold": found["error_at_threshold"],
            }
    metrics["combined"] = {"sensitive_recall": approach["sensitive_recall_combined"]}
    return metrics


def stage(
    candidates: Path, folder: Path, config: IntentConfig
) -> tuple[IntentManifest, dict[str, Any]]:
    """Writes the files, the card and the manifest last into `folder`."""
    evaluation = json.loads((candidates / EVALUATION_FILE).read_bytes())
    approach = winner_of(evaluation)
    source = candidate_dir(approach, candidates)
    try:
        document = read_candidate(source)
    except CandidateError as error:
        raise PublishError("publish.candidate_changed", error.rule) from None
    _require_clean(document, evaluation)
    for name, data in model_files(document, source).items():
        (folder / name).write_bytes(data)
    measured = evaluation["approaches"][approach.value]
    (folder / CARD_FILE).write_text(
        render_card(approach, document.labels, measured, evaluation, config), encoding="utf-8"
    )
    lineage = evaluation["lineage"]
    manifest = IntentManifest(
        model_version=config.model.version,
        approach=approach,
        input=INPUT_CONTRACT,
        labels=document.labels,
        temperature=document.temperature,
        threshold=document.threshold,
        dataset=DatasetLineage(
            repository=config.dataset.repository,
            version=config.dataset.version,
            revision=config.dataset.revision,
        ),
        human_test_sha256=lineage["human_test_sha256"],
        embedder=(
            {"repository": MODEL_REPOSITORY, "revision": MODEL_REVISION}
            if approach is Approach.E5
            else None
        ),
        code_commit=lineage["code_commit"],
        training_run_id=document.mlflow_run_id,
        evaluation_run_id=lineage.get("mlflow_run_id"),
        metrics=manifest_metrics(measured),
        latency_ms={"p50": measured["latency_ms"]["p50"], "p95": measured["latency_ms"]["p95"]},
        files=tuple(
            ManifestFile(name=path.name, size_bytes=path.stat().st_size, sha256=sha256_of(path))
            for path in sorted(folder.iterdir())
        ),
    )
    (folder / MANIFEST_FILE).write_bytes(canonical_json(manifest))
    return manifest, evaluation


def plan_lines(manifest: IntentManifest, folder: Path, repo_id: str, tag: str) -> list[str]:
    manifest_path = folder / MANIFEST_FILE
    return [
        f"Vencedor: {manifest.approach.value}",
        f"Destino: https://huggingface.co/{repo_id} (tag {tag})",
        "Arquivos:",
        *[f"  {f.name:<18} {f.size_bytes:>11} bytes  {f.sha256}" for f in manifest.files],
        f"  {MANIFEST_FILE:<18} {manifest_path.stat().st_size:>11} bytes  "
        f"{sha256_of(manifest_path)}",
    ]


def _interval(value: dict[str, float]) -> str:
    return f"{value['point']:.3f} [{value['low']:.3f}, {value['high']:.3f}]"


def _number(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_card(
    approach: Approach,
    labels: tuple[str, ...],
    measured: dict[str, Any],
    evaluation: dict[str, Any],
    config: IntentConfig,
) -> str:
    rows = [
        f"| {name} | {_interval(m['macro_f1'])} | {_number(m['sensitive_recall'])} "
        f"| {_number(m['ece'])} | {_number(m['coverage_at_threshold'])} |"
        for name, m in measured["sets"].items()
    ]
    comparison = [
        f"| {name} | {_number(evaluation['choice']['human_macro_f1'].get(name))} "
        f"| {_number(a['sensitive_recall_combined'])} | {a['latency_ms']['p95']:.1f} "
        f"| {'yes' if name in evaluation['choice']['eligible'] else 'no'} |"
        for name, a in evaluation["approaches"].items()
    ]
    lineage = evaluation["lineage"]
    lines = [
        "---",
        "license: mit",
        "language: [pt]",
        "library_name: deliverius",
        "tags: [text-classification, intent-classification, customer-support, synthetic-data]",
        f"datasets: [{config.dataset.repository}]",
        "---",
        "",
        "# Deliverius intent classifier",
        "",
        "Classifies the first message of a customer of Deliverius, a fictional food delivery",
        "app, into one of 11 intents, with a calibrated confidence and the threshold under which",
        "the assistant asks a clarifying question instead of acting.",
        f"Approach: `{approach.value}`.",
        "",
        "> **Synthetic data only.** Training data was generated, and no person wrote any test:",
        "> the final test holds simulated customers, written by a model of another family. Do not",
        "> use it to decide anything about real people.",
        "",
        "## Input contract",
        "",
        "The input is the output of `deliverius.privacy.mask_text`, the masking the application",
        "applies before classification: documents, phones, e-mails and addresses are replaced by",
        "markers such as `[CPF_1]`. Proper names are not masked (a documented limit).",
        "",
        "## Labels",
        "",
        ", ".join(f"`{label}`" for label in labels),
        "",
        "## How it was chosen",
        "",
        "The rule was written before any test run. Eligible: recall of `sensitive` of at least",
        f"{config.rule.min_sensitive_recall} over the three test sets together, and p95 latency",
        f"of at most {config.rule.max_p95_ms:g} ms in CPU. Among the eligible, the best macro F1",
        "on the simulated-customer test; a paired bootstrap interval of the difference that holds",
        "zero is a tie, and the fastest of the tied wins.",
        "",
        "| Approach | Simulated macro F1 | Sensitive recall (3 tests) | p95 ms | Eligible |",
        "| --- | --- | --- | --- | --- |",
        *comparison,
        "",
        "## Metrics of this model",
        "",
        "| Set | Macro F1 [95% CI] | Sensitive recall | ECE | Coverage at threshold |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        "## Limitations",
        "",
        "- The validation set is synthetic, so the router threshold may be loose on real text.",
        "- The simulated-customer test is small (its intervals are wide). Claude agents wrote it,",
        "  acting as three customer personas: a model family other than the generators of the",
        "  training set (Gemini) and of the holdout (Groq). It measures generalization to an",
        "  unseen author, not to real customers.",
        "- Latency was measured one message at a time, on one CPU thread.",
        "",
        "## Files",
        "",
        "Numbers and text only (JSON, ONNX, a tokenizer), no pickle; `manifest.json` holds the",
        "SHA-256 of every file.",
        "",
        "## Lineage",
        "",
        f"- Dataset `{config.dataset.repository}` {config.dataset.version}, commit "
        f"`{config.dataset.revision}`.",
        f"- Human test SHA-256 `{lineage['human_test_sha256']}`.",
        f"- Code commit `{lineage['code_commit']}`, seed {lineage['seed']}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, hub_for: HubFactory = huggingface_model_hub) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.publish")
    parser.add_argument("--candidates", type=Path, default=CANDIDATES_DIR)
    parser.add_argument("--confirm", action="store_true", help="envia de verdade")
    args = parser.parse_args(argv)
    use_utf8_output()
    config = load_config()
    repo_id, tag = config.model.repository, f"v{config.model.version}"

    def staged(folder: Path) -> Staged:
        manifest, _ = stage(args.candidates, folder, config)
        return Staged(
            Upload(repo_id, tag, folder, f"Intent model {tag} ({manifest.approach.value})"),
            tuple(plan_lines(manifest, folder, repo_id, tag)),
        )

    code = publish(staged, confirm=args.confirm, hub_for=hub_for, prefix="deliverius-intent-")
    if code == 0 and args.confirm:
        logger.info("intent_research.published", repository=repo_id, tag=tag)
    return code


if __name__ == "__main__":
    sys.exit(main())
