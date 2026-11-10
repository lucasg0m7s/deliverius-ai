"""The research modules the CI runs must import without the `finetune` group.

CI does not install torch, transformers or onnx. A module-level import of one of them, even through
another library (`onnxruntime.quantization` imports `onnx`), breaks test collection there while
passing on a machine that has the group installed. A child process with those packages blocked
reproduces the CI environment on any machine.
"""

import subprocess
import sys

import pytest

FINETUNE_PACKAGES = ("torch", "transformers", "datasets", "accelerate", "onnx", "onnxscript")
CI_MODULES = (
    "research.intent.evaluate",
    "research.intent.export",
    "research.intent.publish",
    "research.intent.finetune",
)


# A finder first in `sys.meta_path` makes the packages look absent, as on CI: `find_spec` answers
# `None` and an import raises `ModuleNotFoundError`. (`sys.modules[name] = None` would not do: some
# libraries see the name in `sys.modules` and use it.)
_CHILD = """
import importlib, importlib.abc, sys
BLOCKED = {blocked!r}
class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(name, name=name)
        return None
sys.meta_path.insert(0, Absent())
importlib.import_module({module!r})
"""


@pytest.mark.parametrize("module", CI_MODULES)
def test_module_imports_without_the_finetune_group(module: str) -> None:
    code = _CHILD.format(blocked=FINETUNE_PACKAGES, module=module)

    result = subprocess.run(  # noqa: S603 - fixed interpreter and code, no outside input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=120
    )

    assert result.returncode == 0, result.stderr
