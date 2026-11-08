import subprocess
import sys

HEAVY_PACKAGES = ("torch", "transformers", "optimum", "sentence_transformers")


def test_importing_retrieval_loads_neither_torch_nor_transformers() -> None:
    script = (
        "import sys, deliverius.retrieval, deliverius.retrieval.embedder\n"
        f"loaded = [name for name in {HEAVY_PACKAGES!r} if name in sys.modules]\n"
        "print(','.join(loaded))\n"
    )

    result = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60
    )

    assert result.stdout.strip() == ""
