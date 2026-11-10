"""Approach 3: fine-tuning BERTimbau, run on a Kaggle GPU by the notebook, which only calls this.

Two commands:

- `python -m research.intent.finetune search --out DIR`: one seed, each learning rate of the
  grid; prints the rate with the best validation macro F1.
- `python -m research.intent.finetune seeds --learning-rate LR --out DIR`: the seeds of the
  config at that rate.

Each run trains up to `max_epochs`, keeps the epoch with the best validation macro F1 (early
stopping with patience 1) and writes a folder with the weights in safetensors, `config.json`,
`tokenizer.json`, `epochs.json` and `result.json`. Only training and validation are read; the
human test never goes to Kaggle. Export, calibration and evaluation run locally (`export.py`).

The pure parts (label maps, the 128-or-256 rule, the run settings) import no torch and run in CI;
torch and transformers are imported inside the training function, from the `finetune` group.
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from deliverius.cli.console import use_utf8_output
from research.dataset import fetch_dataset
from research.intent.config import FinetuneSection, load_config
from research.intent.data import LABELS, Corpus, MessageSet, load_synthetic
from research.intent.metrics import macro_f1

EPOCHS_FILE: Final = "epochs.json"
RESULT_FILE: Final = "result.json"
METRIC: Final = "macro_f1"


def label_maps() -> tuple[dict[int, str], dict[str, int]]:
    id2label = dict(enumerate(LABELS))
    return id2label, {label: index for index, label in id2label.items()}


def long_share(token_lengths: Sequence[int], short_max_length: int) -> float:
    """The share of messages longer than `short_max_length` tokens (special tokens included)."""
    if not token_lengths:
        return 0.0
    return sum(1 for length in token_lengths if length > short_max_length) / len(token_lengths)


def choose_max_length(token_lengths: Sequence[int], section: FinetuneSection) -> int:
    """128 tokens, unless more than the threshold share of validation messages is longer."""
    share = long_share(token_lengths, section.short_max_length)
    return (
        section.long_max_length
        if share > section.long_share_threshold
        else section.short_max_length
    )


@dataclass(frozen=True, slots=True)
class RunSettings:
    """One training run: the fixed choices of the config plus this run's rate, seed and length."""

    base_model: str
    base_revision: str | None
    learning_rate: float
    seed: int
    max_length: int
    max_epochs: int
    patience: int
    batch_size: int
    warmup_ratio: float
    weight_decay: float

    @classmethod
    def of(
        cls, section: FinetuneSection, learning_rate: float, seed: int, max_length: int
    ) -> "RunSettings":
        return cls(
            base_model=section.base_model,
            base_revision=section.base_revision,
            learning_rate=learning_rate,
            seed=seed,
            max_length=max_length,
            max_epochs=section.max_epochs,
            patience=section.patience,
            batch_size=section.batch_size,
            warmup_ratio=section.warmup_ratio,
            weight_decay=section.weight_decay,
        )

    @property
    def name(self) -> str:
        return f"lr{self.learning_rate:g}-seed{self.seed}"


def best_rate(results: Sequence[dict[str, Any]]) -> float:
    """The learning rate of the best validation macro F1; a tie keeps the smaller rate."""
    best = max(sorted(results, key=lambda r: r["learning_rate"]), key=lambda r: r[METRIC])
    return float(best["learning_rate"])


def train_run(
    settings: RunSettings, train: MessageSet, validation: MessageSet, out: Path
) -> dict[str, Any]:
    """Trains one run and writes its folder; returns `result.json`. Needs the `finetune` group."""
    import torch  # noqa: PLC0415 - the `finetune` group is not installed in CI
    from datasets import Dataset  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoModelForSequenceClassification,
        AutoTokenizer,
        DataCollatorWithPadding,
        EarlyStoppingCallback,
        EvalPrediction,
        Trainer,
        TrainingArguments,
        set_seed,
    )

    set_seed(settings.seed)
    id2label, label2id = label_maps()
    tokenizer = AutoTokenizer.from_pretrained(settings.base_model, revision=settings.base_revision)
    model = AutoModelForSequenceClassification.from_pretrained(
        settings.base_model,
        revision=settings.base_revision,
        num_labels=len(LABELS),
        id2label=id2label,
        label2id=label2id,
    )

    def encoded(messages: MessageSet) -> Any:
        data = Dataset.from_dict({"text": messages.texts, "label": messages.labels.tolist()})
        return data.map(
            lambda batch: tokenizer(batch["text"], truncation=True, max_length=settings.max_length),
            batched=True,
            remove_columns=["text"],
        )

    def compute_metrics(prediction: EvalPrediction) -> dict[str, float]:
        predicted = np.asarray(prediction.predictions).argmax(axis=1).astype(np.int64)
        labels = np.asarray(prediction.label_ids).astype(np.int64)
        return {METRIC: macro_f1(labels, predicted, len(LABELS))}

    with tempfile.TemporaryDirectory(prefix="deliverius-finetune-") as checkpoints:
        arguments = TrainingArguments(
            output_dir=checkpoints,
            eval_strategy="epoch",
            save_strategy="epoch",
            logging_strategy="epoch",
            load_best_model_at_end=True,
            metric_for_best_model=METRIC,
            greater_is_better=True,
            save_total_limit=2,
            num_train_epochs=settings.max_epochs,
            learning_rate=settings.learning_rate,
            per_device_train_batch_size=settings.batch_size,
            per_device_eval_batch_size=64,
            # A float below 1 is a share of the training steps in this version of transformers.
            warmup_steps=settings.warmup_ratio,
            weight_decay=settings.weight_decay,
            fp16=torch.cuda.is_available(),
            seed=settings.seed,
            data_seed=settings.seed,
            report_to="none",
        )
        trainer = Trainer(
            model=model,
            args=arguments,
            train_dataset=encoded(train),
            eval_dataset=encoded(validation),
            processing_class=tokenizer,
            data_collator=DataCollatorWithPadding(tokenizer),
            compute_metrics=compute_metrics,
            callbacks=[EarlyStoppingCallback(early_stopping_patience=settings.patience)],
        )
        trainer.train()
        best = trainer.evaluate()
        out.mkdir(parents=True, exist_ok=True)
        trainer.save_model(str(out))
        tokenizer.save_pretrained(str(out))
        # A pickle of the arguments; `result.json` already holds them as text.
        (out / "training_args.bin").unlink(missing_ok=True)
        epochs = trainer.state.log_history
    result = asdict(settings) | {
        METRIC: float(best[f"eval_{METRIC}"]),
        "best_checkpoint_epoch": _best_epoch(epochs),
        "torch": torch.__version__,
    }
    (out / EPOCHS_FILE).write_text(json.dumps(epochs, indent=2), encoding="utf-8")
    (out / RESULT_FILE).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _best_epoch(epochs: Sequence[dict[str, Any]]) -> float | None:
    scored = [e for e in epochs if f"eval_{METRIC}" in e]
    return max(scored, key=lambda e: e[f"eval_{METRIC}"]).get("epoch") if scored else None


def validation_lengths(base_model: str, revision: str | None, texts: Sequence[str]) -> list[int]:
    """Token counts of the validation messages, special tokens included, without truncation."""
    from transformers import AutoTokenizer  # noqa: PLC0415 - the `finetune` group

    tokenizer = AutoTokenizer.from_pretrained(base_model, revision=revision)
    return [len(ids) for ids in tokenizer(list(texts))["input_ids"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.finetune")
    parser.add_argument("mode", choices=["search", "seeds"])
    parser.add_argument("--out", type=Path, required=True, help="pasta das execuções")
    parser.add_argument("--learning-rate", type=float, help="taxa escolhida (modo seeds)")
    args = parser.parse_args(argv)
    use_utf8_output()
    config = load_config()
    section = config.finetune
    sets = load_synthetic(fetch_dataset(config.dataset))
    train, validation = sets[Corpus.TRAIN], sets[Corpus.VALIDATION]
    lengths = validation_lengths(section.base_model, section.base_revision, validation.texts)
    max_length = choose_max_length(lengths, section)
    sys.stdout.write(
        f"Mensagens da validação acima de {section.short_max_length} tokens: "
        f"{long_share(lengths, section.short_max_length):.1%}; sequência máxima {max_length}.\n"
    )
    if args.mode == "search":
        plan = [(rate, section.search_seed) for rate in section.learning_rates]
    elif args.learning_rate is None:
        parser.error("o modo seeds precisa de --learning-rate")
    else:
        plan = [(args.learning_rate, seed) for seed in section.seeds]
    results = []
    for rate, seed in plan:
        settings = RunSettings.of(section, rate, seed, max_length)
        result = train_run(settings, train, validation, args.out / settings.name)
        results.append(result)
        sys.stdout.write(f"{settings.name}: F1 macro na validação {result[METRIC]:.4f}\n")
    if args.mode == "search":
        sys.stdout.write(f"Taxa escolhida: {best_rate(results):g}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
