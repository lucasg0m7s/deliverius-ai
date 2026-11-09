"""Publishes the chosen fraud model to the Hugging Face Hub, with its card and manifest.

`python -m research.fraud.publish` is a dry run: it builds the folder and shows the files, their
hashes and the destination, and sends nothing. `--confirm` creates the repository, uploads and
tags the commit with the model version. A version already tagged is refused before any upload; a
run that died between the upload and the tag is simply run again. The card is in English because
the model is public; what the script tells the author is Portuguese.
"""

import argparse
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from huggingface_hub import HfApi
from pydantic import SecretStr

from deliverius.artifacts import sha256_of
from deliverius.cli.console import use_utf8_output
from deliverius.config import Settings
from deliverius.fraud.features import FEATURE_NAMES, FEATURES_VERSION, schema_hash
from deliverius.fraud.model_files import (
    CARD_FILE,
    DatasetLineage,
    FraudManifest,
    ManifestFile,
)
from deliverius.shared.errors import DeliveriusError
from research.fraud.candidate import MODEL_FILES, canonical_json, read_candidate
from research.fraud.config import ResearchConfig, load_config
from research.fraud.train import DEFAULT_CANDIDATE

MANIFEST_NAME: Final = "manifest.json"


class PublishError(DeliveriusError):
    """Publishing cannot go on; `rule` is a stable code and `detail` never holds a secret."""

    def __init__(self, rule: str, detail: str | None = None) -> None:
        super().__init__(rule if detail is None else f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


class ModelHub(Protocol):
    """What publishing asks of the Hub; a test replaces it."""

    def create_model_repo(self, repo_id: str) -> None: ...

    def tags(self, repo_id: str) -> frozenset[str]: ...

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        """Uploads every file of `folder` in one commit and returns the commit hash."""
        ...

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None: ...


class HuggingFaceModelHub:
    def __init__(self, api: HfApi) -> None:
        self._api = api

    def create_model_repo(self, repo_id: str) -> None:
        self._api.create_repo(repo_id=repo_id, repo_type="model", private=False, exist_ok=True)

    def tags(self, repo_id: str) -> frozenset[str]:
        refs = self._api.list_repo_refs(repo_id, repo_type="model")
        return frozenset(ref.name for ref in refs.tags)

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        commit = self._api.upload_folder(
            repo_id=repo_id, repo_type="model", folder_path=folder, commit_message=message
        )
        return str(commit.oid)

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None:
        self._api.create_tag(repo_id, tag=tag, revision=revision, repo_type="model")


type HubFactory = Callable[[SecretStr], ModelHub]


def huggingface_model_hub(token: SecretStr) -> ModelHub:
    return HuggingFaceModelHub(HfApi(token=token.get_secret_value()))


@dataclass(frozen=True)
class PublishPlan:
    repo_id: str
    tag: str
    folder: Path
    manifest: FraudManifest
    manifest_size: int
    manifest_sha256: str


def stage(candidate_dir: Path, folder: Path, config: ResearchConfig) -> PublishPlan:
    """Writes the files to publish into `folder`: model files, card, and the manifest last."""
    # Reading the candidate validates every number before anything is staged.
    _, research = read_candidate(candidate_dir)
    if research.get("code_dirty", True):
        raise PublishError("publish.dirty_code", str(research.get("code_commit")))
    if research.get("feature_schema_hash") != schema_hash():
        raise PublishError("publish.schema_changed", "train again with the current features")
    for name in MODEL_FILES:
        (folder / name).write_bytes((candidate_dir / name).read_bytes())
    (folder / CARD_FILE).write_text(render_card(research, config), encoding="utf-8")
    manifest = FraudManifest(
        model_version=config.model.version,
        model_kind="logistic_regression",
        features_version=FEATURES_VERSION,
        feature_schema_hash=schema_hash(),
        dataset=DatasetLineage(
            repository=config.dataset.repository,
            version=config.dataset.version,
            revision=config.dataset.revision,
        ),
        mlflow_run_id=research["mlflow_run_id"],
        code_commit=research["code_commit"],
        metrics=_manifest_metrics(research),
        files=tuple(
            ManifestFile(
                name=name,
                size_bytes=(folder / name).stat().st_size,
                sha256=sha256_of(folder / name),
            )
            for name in (*MODEL_FILES, CARD_FILE)
        ),
    )
    (folder / MANIFEST_NAME).write_bytes(canonical_json(manifest))
    return PublishPlan(
        repo_id=config.model.repository,
        tag=f"v{config.model.version}",
        folder=folder,
        manifest=manifest,
        manifest_size=(folder / MANIFEST_NAME).stat().st_size,
        manifest_sha256=sha256_of(folder / MANIFEST_NAME),
    )


def send(plan: PublishPlan, hub: ModelHub) -> str:
    """Upload and tag; returns the commit to pin as `REVISION`. Never overwrites a version."""
    hub.create_model_repo(plan.repo_id)
    if plan.tag in hub.tags(plan.repo_id):
        raise PublishError("publish.tag_exists", plan.tag)
    revision = hub.upload_folder(plan.repo_id, plan.folder, f"Fraud model {plan.tag}")
    hub.create_tag(plan.repo_id, plan.tag, revision)
    return revision


def plan_lines(plan: PublishPlan) -> list[str]:
    return [
        f"Destino: https://huggingface.co/{plan.repo_id} (tag {plan.tag})",
        "Arquivos:",
        *[f"  {f.name:<16} {f.size_bytes:>8} bytes  {f.sha256}" for f in plan.manifest.files],
        f"  {MANIFEST_NAME:<16} {plan.manifest_size:>8} bytes  {plan.manifest_sha256}",
        "Fixar em src/deliverius/fraud/model_files.py:",
        f'  MANIFEST = ModelFile("{MANIFEST_NAME}", {plan.manifest_size}, '
        f'"{plan.manifest_sha256}")',
    ]


def main(argv: list[str] | None = None, hub_for: HubFactory = huggingface_model_hub) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.fraud.publish")
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--confirm", action="store_true", help="envia de verdade")
    args = parser.parse_args(argv)
    use_utf8_output()
    config = load_config()
    with tempfile.TemporaryDirectory(prefix="deliverius-fraud-") as staging:
        try:
            plan = stage(args.candidate, Path(staging), config)
            for line in plan_lines(plan):
                sys.stdout.write(f"{line}\n")
            if not args.confirm:
                sys.stdout.write("Ensaio: nada foi enviado. Use --confirm para publicar.\n")
                return 0
            revision = _send_reporting_only_the_type(plan, hub_for(_token()))
        except PublishError as error:
            sys.stderr.write(f"Publicação recusada: {error}\n")
            return 1
    sys.stdout.write(f'Publicado. Fixar REVISION = "{revision}".\n')
    return 0


def _token() -> SecretStr:
    token = Settings().hf_token
    if token is None:
        raise PublishError("publish.token_missing", "HF_TOKEN")
    return token


def _send_reporting_only_the_type(plan: PublishPlan, hub: ModelHub) -> str:
    try:
        return send(plan, hub)
    except PublishError:
        raise
    except Exception as error:
        # The library's error text can carry request details: only the type is kept.
        raise PublishError("publish.hub_failed", type(error).__name__) from None


def _manifest_metrics(research: dict[str, Any]) -> dict[str, dict[str, float]]:
    test = research["test"]["models"]["logistic"]
    return {
        "validation": {
            "pr_auc": research["validation_pr_auc"]["logistic"]["point"],
            "pr_auc_minus_lightgbm": -research["lightgbm_minus_logistic"]["point"],
        },
        "test": {
            "pr_auc": test["pr_auc"]["point"],
            "pr_auc_low": test["pr_auc"]["low"],
            "pr_auc_high": test["pr_auc"]["high"],
            "roc_auc": test["roc_auc"],
            "brier": test["brier"],
            "recall_at_5pct": test["recall_at_5pct"],
            "cost_per_claim_cents": research["test"]["costs_cents"]["cost_rule"]["per_claim"],
        },
    }


def _money(cents: float) -> str:
    return f"R$ {cents / 100:.2f}"


def render_card(research: dict[str, Any], config: ResearchConfig) -> str:
    test = research["test"]
    models, costs = test["models"], test["costs_cents"]
    logistic, lightgbm, rule = models["logistic"], models["lightgbm"], models["business_rule"]

    def interval(value: dict[str, float]) -> str:
        return f"{value['point']:.3f} [{value['low']:.3f}, {value['high']:.3f}]"

    cost_rows = [
        ("Never review", "never"),
        ("Review everything", "always"),
        (f"Business rule ({config.business_rule_claims_30d}+ claims in 30 days)", "business_rule"),
        ("Fixed threshold (TunedThresholdClassifierCV)", "tuned_threshold"),
        ("**Cost rule: p x amount >= review cost**", "cost_rule"),
    ]
    lines = [
        "---",
        "license: mit",
        "library_name: deliverius",
        "tags: [tabular-classification, fraud-detection, synthetic-data, calibration]",
        f"datasets: [{config.dataset.repository}]",
        "---",
        "",
        "# Deliverius refund fraud model",
        "",
        "A calibrated probability that a refund, coupon or cancellation claim on the",
        "fictional Deliverius delivery app is fraudulent, plus the rule that sends a claim to a",
        "human reviewer when the expected loss reaches the cost of a review. It is a logistic",
        "regression over 27 point-in-time features, calibrated with Platt scaling.",
        "",
        "> **Synthetic data only.** Every customer, order and claim was generated. The model",
        "> learned the generator, not real fraud. Do not use it to decide anything about real",
        "> people.",
        "",
        "## Intended use",
        "",
        "- Demonstrating a reproducible tabular ML pipeline: time split, baselines, calibration,",
        "  cost-based decision, pinned artifacts and a CI quality gate.",
        "- Scoring claims inside the Deliverius demo, where a person reviews every flagged case.",
        "",
        "**Not intended:** decisions about real customers, credit or identity checks, or any use",
        "where a score alone denies someone a refund.",
        "",
        "## Data",
        "",
        f"- Dataset: [`{config.dataset.repository}`](https://huggingface.co/datasets/"
        f"{config.dataset.repository}) version {config.dataset.version}, commit "
        f"`{config.dataset.revision}`.",
        "- Population: claims the policy engine did not deny (denied claims are never scored).",
        "- Split by time: training May to August (hyperparameters by expanding monthly folds),",
        "  validation September (model choice, calibration, threshold), test October (once).",
        f"- Test set: {test['claims']} claims, {test['frauds']} frauds.",
        "",
        "## Features",
        "",
        "Only facts strictly before the claim instant; no name, address, document, message",
        "text, customer profile or reviewer outcome. Missing values get the training median.",
        "",
        ", ".join(f"`{name}`" for name in FEATURE_NAMES),
        "",
        f"Feature schema version {FEATURES_VERSION}, hash `{schema_hash()}`.",
        "",
        "## How the model was chosen",
        "",
        "The rule was written before the first run and uses validation only: LightGBM is chosen",
        "when the 95% paired-bootstrap interval of PR-AUC(LightGBM) - PR-AUC(logistic) is entirely",
        "above zero; otherwise the simpler model stays. On September the interval was "
        f"[{research['lightgbm_minus_logistic']['low']:.3f}, "
        f"{research['lightgbm_minus_logistic']['high']:.3f}], so the logistic regression stayed.",
        "",
        "## Test metrics (October)",
        "",
        "| Scorer | PR-AUC [95% CI] | ROC-AUC | Brier | Recall at top 5% |",
        "| --- | --- | --- | --- | --- |",
        f"| **Logistic (calibrated)** | {interval(logistic['pr_auc'])} | {logistic['roc_auc']:.3f} "
        f"| {logistic['brier']:.4f} | {logistic['recall_at_5pct']:.3f} |",
        f"| LightGBM (calibrated) | {interval(lightgbm['pr_auc'])} | {lightgbm['roc_auc']:.3f} "
        f"| {lightgbm['brier']:.4f} | {lightgbm['recall_at_5pct']:.3f} |",
        f"| Business rule | {interval(rule['pr_auc'])} | {rule['roc_auc']:.3f} | - "
        f"| {rule['recall_at_5pct']:.3f} |",
        "",
        f"Expected cost per claim, with a review costing {_money(config.review_cost_cents)}:",
        "",
        "| Rule | Reviewed | Frauds caught | Cost per claim |",
        "| --- | --- | --- | --- |",
        *[
            f"| {label} | {costs[key]['reviewed']} | {costs[key]['frauds_caught']} "
            f"| {_money(costs[key]['per_claim'])} |"
            for label, key in cost_rows
        ],
        "",
        "## Files",
        "",
        "Plain JSON with numbers only, no pickle: loading the model runs no code.",
        "",
        "- `logistic.json`: medians, means, scales, coefficients and intercept, in feature order.",
        "- `calibrator.json`: `p = 1 / (1 + exp(a * raw + b))` over the raw score.",
        "- `decision.json`: the cost of one review, in cents.",
        "- `manifest.json`: versions, lineage, metrics and the SHA-256 of every file.",
        "",
        "## Limitations",
        "",
        "- Synthetic data: the signal is the generator's, and real fraud is more varied.",
        f"- Few positives ({test['frauds']} frauds in the test set): the intervals are wide.",
        "- Orders start on May 1st, so the 90-day windows are incomplete until the end of July;",
        "  validation and test have complete windows.",
        "- The calibration is sigmoid: about 60 positives per month are too few for isotonic.",
        "",
        "## Lineage",
        "",
        f"- Code commit `{research['code_commit']}`, MLflow run `{research['mlflow_run_id']}`,",
        f"  seed {research['seed']}.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
