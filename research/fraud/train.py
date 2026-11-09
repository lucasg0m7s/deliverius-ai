"""Trains and compares the fraud models, following the choice rule written before the first run.

`python -m research.fraud.train` runs everything once: the business rule, the logistic regression
and LightGBM (hyperparameters by monthly folds inside training), the choice on the September
validation, the sigmoid calibration and the tuned threshold on September, and the October test,
computed once after the choice. The run goes to MLflow and the chosen model to a candidate folder.
"""

import argparse
import asyncio
import itertools
import logging
import math
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import lightgbm as lgb
import matplotlib.pyplot as plt
import mlflow
import numpy as np
import shap
import sklearn
import structlog
from numpy.typing import NDArray
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import make_scorer
from sklearn.model_selection import TunedThresholdClassifierCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from deliverius.cli.commit import read_commit_info
from deliverius.cli.console import use_utf8_output
from deliverius.datagen.output import read_manifest
from deliverius.datagen.pipeline import CommitInfo
from deliverius.fraud.decision import needs_review
from deliverius.fraud.features import FEATURE_NAMES, FEATURES_VERSION, schema_hash
from deliverius.fraud.scorer import LogisticFraudScorer
from deliverius.shared.money import Money
from research.fraud.candidate import (
    FIGURES_DIR,
    MODEL_FILES,
    RESEARCH_FILE,
    Candidate,
    candidate_from,
    canonical_json,
    write_candidate,
)
from research.fraud.config import LightGbmSection, ResearchConfig, load_config
from research.fraud.dataset import ClaimTable, load_claim_table
from research.fraud.evaluate import (
    Interval,
    ReliabilityBin,
    RuleCost,
    brier,
    paired_pr_auc_difference,
    plot_precision_recall,
    plot_reliability,
    pr_auc,
    pr_auc_interval,
    recall_at_top,
    reliability,
    roc_auc,
    rule_cost,
)
from research.fraud.split import TimeSplit, split_by_month

type ModelName = Literal["logistic", "lightgbm"]
type Floats = NDArray[np.float64]

CLAIMS_30D: Final = FEATURE_NAMES.index("claims_30d")


class ResearchStop(Exception):  # noqa: N818 - a deliberate stop of the study, not a bug
    """The study cannot go on as written (`rule` says why): the case goes back to analysis."""

    def __init__(self, rule: str, detail: str = "") -> None:
        super().__init__(f"{rule}: {detail}" if detail else rule)
        self.rule = rule


class ProbabilityMismatchError(AssertionError):
    """Two computations of the same probabilities disagree beyond the tolerance."""

    def __init__(self, worst: float, tolerance: float) -> None:
        super().__init__(f"probabilities differ by {worst} (tolerance {tolerance})")


@dataclass(frozen=True)
class ModelReport:
    """Test metrics of one scorer; `brier` and `reliability` only exist for probabilities."""

    pr_auc: Interval
    roc_auc: float
    recall_at_5pct: float
    brier: float | None
    reliability: tuple[ReliabilityBin, ...]


@dataclass(frozen=True)
class ResearchResult:
    best_params: dict[str, Any]
    fold_pr_auc: float
    validation: dict[str, Interval]
    lightgbm_minus_logistic: Interval
    chosen: ModelName
    chosen_minus_rule: Interval
    calibrator_a: float
    calibrator_b: float
    tuned_threshold: float
    test: dict[str, ModelReport]
    test_costs: dict[str, RuleCost]
    test_claims: int
    test_frauds: int
    test_probabilities: dict[str, Floats]
    test_business_rule: NDArray[np.bool_]
    lightgbm: lgb.LGBMClassifier
    logistic: Pipeline
    calibrated: CalibratedClassifierCV


def business_rule(features: Floats, claims_30d: int) -> NDArray[np.bool_]:
    """The fixed rule the models must beat: review when the customer has many recent claims."""
    flagged: NDArray[np.bool_] = features[:, CLAIMS_30D] >= claims_30d
    return flagged


def fit_logistic(x: Floats, y: NDArray[np.int64], config: ResearchConfig) -> Pipeline:
    """Missing values (no delivery, free order) get the training median before scaling."""
    model = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=config.logistic.C, max_iter=config.logistic.max_iter, random_state=config.seed
                ),
            ),
        ]
    )
    return model.fit(x, y)


def lightgbm_grid(section: LightGbmSection) -> Iterator[dict[str, Any]]:
    grid = section.grid
    for leaves, min_child, (rate, estimators), reg_lambda in itertools.product(
        grid.num_leaves,
        grid.min_child_samples,
        grid.learning_rate_and_estimators,
        grid.reg_lambda,
    ):
        yield {
            "num_leaves": leaves,
            "min_child_samples": min_child,
            "learning_rate": rate,
            "n_estimators": estimators,
            "reg_lambda": reg_lambda,
        }


def fit_lightgbm(
    x: Floats, y: NDArray[np.int64], params: dict[str, Any], config: ResearchConfig
) -> lgb.LGBMClassifier:
    """No class weights: calibration and the cost rule need the plain probability."""
    model = lgb.LGBMClassifier(**config.lightgbm.fixed, **params, random_state=config.seed)
    return model.fit(x, y)


def choose_lightgbm_params(
    table: ClaimTable, split: TimeSplit, config: ResearchConfig
) -> tuple[dict[str, Any], float]:
    """The grid point with the best mean PR-AUC over the expanding-window folds."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for params in lightgbm_grid(config.lightgbm):
        fold_scores = [
            pr_auc(
                table.is_fraud[valid],
                _positive(
                    fit_lightgbm(table.features[fit], table.is_fraud[fit], params, config),
                    table.features[valid],
                ),
            )
            for fit, valid in split.folds
        ]
        scored.append((float(np.mean(fold_scores)), params))
    # Ties keep the first grid point: smaller trees come first in the grid.
    best_score, best = max(scored, key=lambda pair: pair[0])
    return best, best_score


def run_research(table: ClaimTable, config: ResearchConfig) -> ResearchResult:
    split = split_by_month(table.claimed_at, config.split)
    x, y = table.features, table.is_fraud
    train, valid, test = split.train, split.validation, split.test

    logistic = fit_logistic(x[train], y[train], config)
    best_params, fold_pr_auc = choose_lightgbm_params(table, split, config)
    lightgbm = fit_lightgbm(x[train], y[train], best_params, config)

    # Choice, on September only.
    resamples, seed = config.bootstrap_resamples, config.seed
    rule_valid = business_rule(x[valid], config.business_rule_claims_30d).astype(np.float64)
    scores_valid = {
        "logistic": _positive(logistic, x[valid]),
        "lightgbm": _positive(lightgbm, x[valid]),
        "business_rule": rule_valid,
    }
    validation = {
        name: pr_auc_interval(y[valid], score, resamples, seed)
        for name, score in scores_valid.items()
    }
    lgbm_vs_logistic = paired_pr_auc_difference(
        y[valid], scores_valid["lightgbm"], scores_valid["logistic"], resamples, seed
    )
    chosen: ModelName = "lightgbm" if lgbm_vs_logistic.above_zero else "logistic"
    chosen_vs_rule = paired_pr_auc_difference(
        y[valid], scores_valid[chosen], rule_valid, resamples, seed
    )
    if not chosen_vs_rule.above_zero:
        raise ResearchStop("research.model_below_business_rule", f"{chosen}: {chosen_vs_rule}")

    models = {"logistic": logistic, "lightgbm": lightgbm}
    calibrated = {
        name: CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid").fit(
            x[valid], y[valid]
        )
        for name, model in models.items()
    }
    threshold = tune_threshold(
        calibrated[chosen], x[valid], y[valid], table.requested_cents[valid], config
    )

    # The test set, computed once, after the choice.
    y_test, amounts = y[test], table.requested_cents[test]
    probabilities = {name: _positive(model, x[test]) for name, model in calibrated.items()}
    rule_test = business_rule(x[test], config.business_rule_claims_30d)
    reports = {
        name: _probability_report(y_test, probability, resamples, seed)
        for name, probability in probabilities.items()
    }
    reports["business_rule"] = _ranking_report(
        y_test, rule_test.astype(np.float64), resamples, seed
    )
    chosen_probability = probabilities[chosen]
    review_cost = Money(config.review_cost_cents)
    reviews = {
        "never": np.zeros(len(y_test), dtype=np.bool_),
        "always": np.ones(len(y_test), dtype=np.bool_),
        "business_rule": rule_test,
        "tuned_threshold": chosen_probability >= threshold,
        "cost_rule": np.array(
            [
                needs_review(float(p), Money(int(cents)), review_cost)
                for p, cents in zip(chosen_probability, amounts, strict=True)
            ],
            dtype=np.bool_,
        ),
    }
    calibrator = calibrated[chosen].calibrated_classifiers_[0].calibrators[0]
    return ResearchResult(
        best_params=best_params,
        fold_pr_auc=fold_pr_auc,
        validation=validation,
        lightgbm_minus_logistic=lgbm_vs_logistic,
        chosen=chosen,
        chosen_minus_rule=chosen_vs_rule,
        calibrator_a=float(calibrator.a_),
        calibrator_b=float(calibrator.b_),
        tuned_threshold=threshold,
        test=reports,
        test_costs={
            name: rule_cost(y_test, amounts, review, config.review_cost_cents)
            for name, review in reviews.items()
        },
        test_claims=len(y_test),
        test_frauds=int(y_test.sum()),
        test_probabilities=probabilities,
        test_business_rule=rule_test,
        lightgbm=lightgbm,
        logistic=logistic,
        calibrated=calibrated[chosen],
    )


def tune_threshold(
    calibrated: CalibratedClassifierCV,
    x: Floats,
    y: NDArray[np.int64],
    requested_cents: NDArray[np.int64],
    config: ResearchConfig,
) -> float:
    """The single fixed cut-off with the lowest cost on September, the rival of the cost rule."""
    review_cost = config.review_cost_cents

    def negative_cost(
        y_true: NDArray[np.int64], y_pred: NDArray[np.int64], amount: NDArray[np.int64]
    ) -> float:
        missed = (y_pred == 0) & (y_true == 1)
        return -float((y_pred == 1).sum() * review_cost + amount[missed].sum())

    # The scorer receives each claim's amount through metadata routing.
    with sklearn.config_context(enable_metadata_routing=True):
        scorer = make_scorer(negative_cost, response_method="predict").set_score_request(
            amount=True
        )
        tuned = TunedThresholdClassifierCV(
            FrozenEstimator(calibrated), scoring=scorer, cv="prefit", refit=False
        ).fit(x, y, amount=requested_cents)
    return float(tuned.best_threshold_)


def _positive(model: Any, x: Floats) -> Floats:
    probability: Floats = model.predict_proba(x)[:, 1]
    return probability


def _probability_report(
    y: NDArray[np.int64], probability: Floats, resamples: int, seed: int
) -> ModelReport:
    ranking = _ranking_report(y, probability, resamples, seed)
    return ModelReport(
        pr_auc=ranking.pr_auc,
        roc_auc=ranking.roc_auc,
        recall_at_5pct=ranking.recall_at_5pct,
        brier=brier(y, probability),
        reliability=reliability(y, probability),
    )


def _ranking_report(y: NDArray[np.int64], score: Floats, resamples: int, seed: int) -> ModelReport:
    return ModelReport(
        pr_auc=pr_auc_interval(y, score, resamples, seed),
        roc_auc=roc_auc(y, score),
        recall_at_5pct=recall_at_top(y, score),
        brier=None,
        reliability=(),
    )


def sigmoid_by_hand(probability: Floats, a: float, b: float) -> Floats:
    """What the application applies, without scikit-learn: `p = 1 / (1 + exp(a * p_raw + b))`."""
    calibrated: Floats = 1.0 / (1.0 + np.exp(a * probability + b))
    return calibrated


def assert_close(first: Floats, second: Floats, tolerance: float = 1e-9) -> None:
    worst = float(np.max(np.abs(first - second))) if len(first) else 0.0
    if not worst <= tolerance or math.isnan(worst):
        raise ProbabilityMismatchError(worst, tolerance)


# --- The single full run: `python -m research.fraud.train` -------------------------------------

DEFAULT_CANDIDATE: Final = Path("models/fraud-candidate")
TRACKING_DIR: Final = Path("mlruns")
EXPERIMENT: Final = "deliverius-fraud"


def check_against_the_application(
    result: ResearchResult, candidate: Candidate, test: ClaimTable
) -> float:
    """The hand-applied calibrator and the application scorer both match scikit-learn to 1e-9."""
    expected = result.test_probabilities["logistic"]
    raw = result.logistic.decision_function(test.features)
    assert_close(sigmoid_by_hand(raw, result.calibrator_a, result.calibrator_b), expected)
    scorer = LogisticFraudScorer(candidate.to_model("candidate"))
    served = np.array([asyncio.run(scorer.score(context)).probability for context in test.contexts])
    assert_close(served, expected)
    return float(np.max(np.abs(served - expected)))


def shap_figure(result: ResearchResult, x_train: Floats, x_test: Floats, path: Path) -> None:
    """Global SHAP of the logistic regression: mean |contribution| per feature on the test set."""
    prepare = result.logistic[:-1]
    background = prepare.transform(x_train)
    explainer = shap.LinearExplainer(
        result.logistic.named_steps["model"],
        shap.maskers.Independent(background, max_samples=len(background)),
    )
    explanation = explainer(prepare.transform(x_test))
    explanation.feature_names = list(FEATURE_NAMES)
    shap.plots.bar(explanation, max_display=12, show=False)
    figure = plt.gcf()
    figure.set_size_inches(7, 5)
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def _interval(value: Interval) -> dict[str, float]:
    return {"point": value.point, "low": value.low, "high": value.high}


def research_summary(
    result: ResearchResult, config: ResearchConfig, commit: CommitInfo, dataset_commit: str
) -> dict[str, Any]:
    return {
        "chosen": result.chosen,
        "seed": config.seed,
        "code_commit": commit.commit,
        "code_dirty": commit.dirty,
        "features_version": FEATURES_VERSION,
        "feature_schema_hash": schema_hash(),
        "dataset": {
            "repository": config.dataset.repository,
            "version": config.dataset.version,
            "revision": config.dataset.revision,
            "generator_commit": dataset_commit,
        },
        "lightgbm_best_params": result.best_params,
        "lightgbm_fold_pr_auc": result.fold_pr_auc,
        "validation_pr_auc": {name: _interval(value) for name, value in result.validation.items()},
        "lightgbm_minus_logistic": _interval(result.lightgbm_minus_logistic),
        "chosen_minus_business_rule": _interval(result.chosen_minus_rule),
        "calibrator": {"a": result.calibrator_a, "b": result.calibrator_b},
        "tuned_threshold": result.tuned_threshold,
        "test": {
            "claims": result.test_claims,
            "frauds": result.test_frauds,
            "models": {name: _report(report) for name, report in result.test.items()},
            "costs_cents": {
                name: {
                    "reviewed": cost.reviewed,
                    "frauds_caught": cost.frauds_caught,
                    "total": cost.total_cents,
                    "per_claim": cost.per_claim(result.test_claims),
                }
                for name, cost in result.test_costs.items()
            },
        },
    }


def _report(report: ModelReport) -> dict[str, Any]:
    return {
        "pr_auc": _interval(report.pr_auc),
        "roc_auc": report.roc_auc,
        "recall_at_5pct": report.recall_at_5pct,
        "brier": report.brier,
        "reliability": [
            {"mean_predicted": b.mean_predicted, "observed": b.observed_rate, "count": b.count}
            for b in report.reliability
        ],
    }


def log_to_mlflow(summary: dict[str, Any], candidate_dir: Path) -> str:
    TRACKING_DIR.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{TRACKING_DIR.as_posix()}/mlflow.db")
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, (TRACKING_DIR / "artifacts").resolve().as_uri())
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run() as run:
        mlflow.log_params(
            {
                "seed": summary["seed"],
                "chosen": summary["chosen"],
                "code_commit": summary["code_commit"],
                "code_dirty": summary["code_dirty"],
                "features_version": summary["features_version"],
                "feature_schema_hash": summary["feature_schema_hash"],
                "dataset_version": summary["dataset"]["version"],
                "dataset_revision": summary["dataset"]["revision"],
                **{f"lightgbm_{k}": v for k, v in summary["lightgbm_best_params"].items()},
            }
        )
        mlflow.log_metrics(_flat_metrics(summary))
        mlflow.log_artifacts(str(candidate_dir / FIGURES_DIR), artifact_path="figures")
        for name in (*MODEL_FILES, RESEARCH_FILE):
            mlflow.log_artifact(str(candidate_dir / name), artifact_path="candidate")
        run_id: str = run.info.run_id
    return run_id


def _flat_metrics(summary: dict[str, Any]) -> dict[str, float]:
    test = summary["test"]
    metrics: dict[str, float] = {
        "lightgbm_fold_pr_auc": summary["lightgbm_fold_pr_auc"],
        "tuned_threshold": summary["tuned_threshold"],
    }
    for name, value in summary["validation_pr_auc"].items():
        metrics[f"validation_pr_auc_{name}"] = value["point"]
    for name, model in test["models"].items():
        metrics[f"test_pr_auc_{name}"] = model["pr_auc"]["point"]
        metrics[f"test_roc_auc_{name}"] = model["roc_auc"]
        metrics[f"test_recall_at_5pct_{name}"] = model["recall_at_5pct"]
        if model["brier"] is not None:
            metrics[f"test_brier_{name}"] = model["brier"]
    for name, cost in test["costs_cents"].items():
        metrics[f"test_cost_per_claim_{name}"] = cost["per_claim"]
    return metrics


def figures_of(
    result: ResearchResult, table: ClaimTable, split: TimeSplit, folder: Path
) -> dict[str, Path]:
    y_test = table.is_fraud[split.test]
    figures = {
        "reliability.png": folder / "reliability.png",
        "precision_recall.png": folder / "precision_recall.png",
        "shap_global.png": folder / "shap_global.png",
    }
    plot_reliability(
        {
            "logistic (calibrated)": result.test["logistic"].reliability,
            "lightgbm (calibrated)": result.test["lightgbm"].reliability,
        },
        figures["reliability.png"],
    )
    plot_precision_recall(
        y_test,
        {
            "logistic": result.test_probabilities["logistic"],
            "lightgbm": result.test_probabilities["lightgbm"],
            "business rule": result.test_business_rule.astype(np.float64),
        },
        figures["precision_recall.png"],
    )
    shap_figure(
        result, table.features[split.train], table.features[split.test], figures["shap_global.png"]
    )
    return figures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.fraud.train")
    parser.add_argument("--out", type=Path, default=DEFAULT_CANDIDATE, help="pasta do candidato")
    args = parser.parse_args(argv)
    use_utf8_output()
    # One `fraud.scored` event per test claim would bury the summary.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    config = load_config()
    commit = read_commit_info()
    table = load_claim_table(config.dataset.directory)
    dataset_commit = read_manifest(config.dataset.directory).commit
    result = run_research(table, config)
    if result.chosen != "logistic":
        raise ResearchStop("research.no_adapter_for_lightgbm")
    candidate = candidate_from(result.logistic, result.calibrated, config.review_cost_cents)
    split = split_by_month(table.claimed_at, config.split)
    largest_gap = check_against_the_application(result, candidate, table.subset(split.test))
    summary = research_summary(result, config, commit, dataset_commit)
    summary["application_max_abs_difference"] = largest_gap
    with tempfile.TemporaryDirectory() as scratch:
        figures = figures_of(result, table, split, Path(scratch))
        write_candidate(args.out, candidate, summary, figures)
    summary["mlflow_run_id"] = log_to_mlflow(summary, args.out)
    (args.out / RESEARCH_FILE).write_bytes(canonical_json(summary))
    sys.stdout.write(f"Modelo escolhido: {result.chosen}. Execução: {summary['mlflow_run_id']}.\n")
    sys.stdout.write(f"Candidato em {args.out}; aplicação = pesquisa até {largest_gap:.1e}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
