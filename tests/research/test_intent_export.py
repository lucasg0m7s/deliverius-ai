"""The export checks: ONNX must compute what PyTorch computes, and int8 must agree with fp32."""

import numpy as np
import pytest

from research.intent.config import load_config
from research.intent.export import ExportMismatchError, check_export

SECTION = load_config().export
Y = np.array([0, 1, 2, 0], dtype=np.int64)
REFERENCE = np.eye(3)[Y] * 5.0


def test_a_faithful_export_keeps_int8() -> None:
    check = check_export(REFERENCE, REFERENCE + 1e-6, REFERENCE * 0.9, Y, SECTION)

    assert check.kept == "int8"
    assert check.int8_argmax_agreement == 1.0
    assert check.summary()["int8_macro_f1_drop"] == 0.0


def test_fp32_logits_beyond_the_tolerance_stop_the_export() -> None:
    with pytest.raises(ExportMismatchError):
        check_export(REFERENCE, REFERENCE + 1e-3, REFERENCE, Y, SECTION)


def test_int8_that_disagrees_too_often_falls_back_to_fp32() -> None:
    flipped = REFERENCE.copy()
    flipped[0] = [0.0, 5.0, 0.0]  # one of four answers changes: 75% agreement

    check = check_export(REFERENCE, REFERENCE, flipped, Y, SECTION)

    assert check.kept == "fp32"
    assert check.int8_argmax_agreement == 0.75
    assert check.int8_macro_f1 < check.fp32_macro_f1
