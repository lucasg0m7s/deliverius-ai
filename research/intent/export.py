"""Exports the fine-tuned BERTimbau to ONNX int8 and freezes it as a candidate, locally, in CPU.

`python -m research.intent.export --checkpoint DIR` (a run folder downloaded from Kaggle):

1. `torch.onnx.export` writes the fp32 graph; its logits must match PyTorch's to the tolerance
   on validation.
2. `onnxruntime.quantization.quantize_dynamic` writes int8 with uint8 per-channel weights, which
   run on any AVX2 CPU (design D4); its argmax must agree with fp32 on at least 99% of validation.
   If it does not, the fp32 graph is kept and the report says so.
3. The temperature and the router threshold are fitted on the kept graph's validation logits.

Inference needs only `onnxruntime` and `tokenizers` (`OnnxIntentModel`), as in the application;
torch is imported inside the export functions, from the `finetune` group.
"""

import argparse
import json
import logging
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import mlflow
import numpy as np
import onnxruntime as ort
import structlog
from onnxruntime.quantization import QuantType, quantize_dynamic
from tokenizers import Tokenizer

from deliverius.cli.commit import read_commit_info
from deliverius.cli.console import use_utf8_output
from research.dataset import fetch_dataset
from research.intent.calibration import calibrate
from research.intent.candidate import (
    CANDIDATES_DIR,
    Approach,
    CandidateDocument,
    candidate_dir,
    write_candidate,
)
from research.intent.config import ExportSection, IntentConfig, load_config
from research.intent.data import LABELS, Corpus, MessageSet, load_synthetic
from research.intent.finetune import RESULT_FILE
from research.intent.metrics import Floats, macro_f1
from research.intent.tracking import numeric_metrics, use_experiment

MODEL_FILE: Final = "model.onnx"
TOKENIZER_FILE: Final = "tokenizer.json"
INPUT_NAMES: Final = ("input_ids", "attention_mask", "token_type_ids")
PAD_TOKEN: Final = "[PAD]"  # noqa: S105 - the padding token of the tokenizer, not a secret
BATCH: Final = 32


class OnnxIntentModel:
    """Masked text to logits with an ONNX session and the checkpoint's `tokenizer.json`."""

    def __init__(self, session: Any, tokenizer: Tokenizer, max_length: int) -> None:
        self._session = session
        self._inputs = tuple(model_input.name for model_input in session.get_inputs())
        self._tokenizer = tokenizer
        self._tokenizer.enable_truncation(max_length=max_length)
        pad_id = tokenizer.token_to_id(PAD_TOKEN)
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=PAD_TOKEN)

    @classmethod
    def load(
        cls, model: Path, tokenizer: Path, max_length: int, threads: int = 1
    ) -> "OnnxIntentModel":
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        return cls(session, Tokenizer.from_file(str(tokenizer)), max_length)

    def logits(self, texts: Sequence[str]) -> Floats:
        batches = [self._batch(texts[i : i + BATCH]) for i in range(0, len(texts), BATCH)]
        return np.concatenate(batches) if batches else np.empty((0, len(LABELS)))

    def _batch(self, texts: Sequence[str]) -> Floats:
        encodings = self._tokenizer.encode_batch(list(texts))
        tensors = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        outputs = self._session.run(None, {name: tensors[name] for name in self._inputs})
        return np.asarray(outputs[0], dtype=np.float64)


def export_fp32(checkpoint: Path, destination: Path, max_length: int) -> None:
    """The fine-tuned model as an ONNX graph of logits, with dynamic batch and sequence."""
    import torch  # noqa: PLC0415 - the `finetune` group is not installed in CI
    from transformers import AutoModelForSequenceClassification  # noqa: PLC0415

    model = AutoModelForSequenceClassification.from_pretrained(str(checkpoint)).eval()

    class Logits(torch.nn.Module):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.model = model

        def forward(self, input_ids: Any, attention_mask: Any, token_type_ids: Any) -> Any:
            return self.model(
                input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids
            ).logits

    example = tuple(torch.ones((2, 8), dtype=torch.int64) for _ in INPUT_NAMES)
    axes = {0: torch.export.Dim("batch"), 1: torch.export.Dim("sequence", max=max_length)}
    torch.onnx.export(
        Logits(),
        example,
        str(destination),
        input_names=list(INPUT_NAMES),
        output_names=["logits"],
        dynamic_shapes=dict.fromkeys(INPUT_NAMES, axes),
        dynamo=True,
        external_data=False,
        verbose=False,
    )


def torch_logits(checkpoint: Path, texts: Sequence[str], max_length: int) -> Floats:
    import torch  # noqa: PLC0415 - the `finetune` group
    from transformers import AutoModelForSequenceClassification, AutoTokenizer  # noqa: PLC0415

    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint))
    model = AutoModelForSequenceClassification.from_pretrained(str(checkpoint)).eval()
    batches = []
    with torch.no_grad():
        for start in range(0, len(texts), BATCH):
            inputs = tokenizer(
                list(texts[start : start + BATCH]),
                truncation=True,
                max_length=max_length,
                padding=True,
                return_tensors="pt",
            )
            batches.append(model(**inputs).logits.double().numpy())
    return np.concatenate(batches)


def quantize_int8(fp32: Path, destination: Path) -> None:
    """Dynamic int8 with uint8 per-channel weights: no VNNI needed and no U8S8 saturation.

    The torch exporter writes intermediate shapes (`value_info`) that the quantizer's own shape
    inference contradicts; they are dropped first and inferred again by the quantizer.
    """
    import onnx  # noqa: PLC0415 - the `finetune` group

    graph = onnx.load(str(fp32))
    del graph.graph.value_info[:]
    without_shapes = destination.with_name(f"{destination.stem}.shapeless.onnx")
    onnx.save(graph, str(without_shapes))
    try:
        quantize_dynamic(
            without_shapes, destination, per_channel=True, weight_type=QuantType.QUInt8
        )
    finally:
        without_shapes.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ExportCheck:
    max_abs_logit_difference: float
    int8_argmax_agreement: float
    fp32_macro_f1: float
    int8_macro_f1: float
    kept: str

    def summary(self) -> dict[str, Any]:
        return {
            "max_abs_logit_difference": self.max_abs_logit_difference,
            "int8_argmax_agreement": self.int8_argmax_agreement,
            "fp32_macro_f1": self.fp32_macro_f1,
            "int8_macro_f1": self.int8_macro_f1,
            "int8_macro_f1_drop": self.fp32_macro_f1 - self.int8_macro_f1,
            "kept_graph": self.kept,
        }


class ExportMismatchError(AssertionError):
    """The ONNX graph does not compute what the PyTorch model computes."""

    def __init__(self, worst: float, tolerance: float) -> None:
        super().__init__(f"ONNX logits differ from PyTorch by {worst} (tolerance {tolerance})")


def check_export(
    reference: Floats, fp32: Floats, int8: Floats, y: Any, section: ExportSection
) -> ExportCheck:
    worst = float(np.max(np.abs(fp32 - reference))) if len(reference) else 0.0
    if not worst <= section.logits_tolerance:
        raise ExportMismatchError(worst, section.logits_tolerance)
    agreement = float((fp32.argmax(axis=1) == int8.argmax(axis=1)).mean())
    return ExportCheck(
        max_abs_logit_difference=worst,
        int8_argmax_agreement=agreement,
        fp32_macro_f1=macro_f1(y, fp32.argmax(axis=1).astype(np.int64), len(LABELS)),
        int8_macro_f1=macro_f1(y, int8.argmax(axis=1).astype(np.int64), len(LABELS)),
        kept="int8" if agreement >= section.min_argmax_agreement else "fp32",
    )


def export_candidate(
    checkpoint: Path, validation: MessageSet, config: IntentConfig, run_id: str | None, root: Path
) -> CandidateDocument:
    run = json.loads((checkpoint / RESULT_FILE).read_text(encoding="utf-8"))
    max_length = int(run["max_length"])
    with tempfile.TemporaryDirectory(prefix="deliverius-export-") as scratch:
        fp32_path, int8_path = Path(scratch) / "fp32.onnx", Path(scratch) / "int8.onnx"
        export_fp32(checkpoint, fp32_path, max_length)
        quantize_int8(fp32_path, int8_path)
        tokenizer = checkpoint / TOKENIZER_FILE
        logits = {
            name: OnnxIntentModel.load(path, tokenizer, max_length).logits(validation.texts)
            for name, path in (("fp32", fp32_path), ("int8", int8_path))
        }
        check = check_export(
            torch_logits(checkpoint, validation.texts, max_length),
            logits["fp32"],
            logits["int8"],
            validation.labels,
            config.export,
        )
        kept = int8_path if check.kept == "int8" else fp32_path
        calibrated = calibrate(logits[check.kept], validation.labels, config)
        commit = read_commit_info()
        return write_candidate(
            candidate_dir(Approach.BERTIMBAU, root),
            CandidateDocument(
                approach=Approach.BERTIMBAU,
                labels=LABELS,
                temperature=calibrated.temperature,
                threshold=calibrated.threshold,
                params={key: run[key] for key in sorted(run)} | {"graph": check.kept},
                seed=int(run["seed"]),
                code_commit=commit.commit,
                code_dirty=commit.dirty,
                dataset_version=config.dataset.version,
                dataset_revision=config.dataset.revision,
                mlflow_run_id=run_id,
                validation=calibrated.summary | check.summary(),
            ),
            {MODEL_FILE: kept.read_bytes(), TOKENIZER_FILE: tokenizer.read_bytes()},
        )


def load_bertimbau(directory: Path, max_length: int, threads: int) -> OnnxIntentModel:
    return OnnxIntentModel.load(
        directory / MODEL_FILE, directory / TOKENIZER_FILE, max_length, threads
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.export")
    parser.add_argument("--checkpoint", type=Path, required=True, help="pasta de uma execução")
    parser.add_argument("--out", type=Path, default=CANDIDATES_DIR, help="pasta dos candidatos")
    args = parser.parse_args(argv)
    use_utf8_output()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    config = load_config()
    validation = load_synthetic(fetch_dataset(config.dataset))[Corpus.VALIDATION]
    use_experiment()
    with mlflow.start_run(run_name="export-bertimbau") as run:
        document = export_candidate(args.checkpoint, validation, config, run.info.run_id, args.out)
        mlflow.log_params(
            {"approach": "bertimbau", **{k: str(v) for k, v in document.params.items()}}
        )
        mlflow.log_metrics(numeric_metrics("validation", document.validation))
    summary = document.validation
    sys.stdout.write(
        f"BERTimbau exportado ({summary['kept_graph']}): F1 macro na validação "
        f"{summary['macro_f1']:.3f}, concordância int8 {summary['int8_argmax_agreement']:.3%}, "
        f"diferença máxima fp32 {summary['max_abs_logit_difference']:.1e}.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
