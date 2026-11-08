"""Production never sets a global LLM cache: it could answer one customer with another call."""

from pathlib import Path

import pytest
from support.name_scan import NameUse, find_name_uses

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "deliverius"
FORBIDDEN = "set_llm_cache"


def test_nothing_in_src_sets_a_global_llm_cache() -> None:
    violations: list[NameUse] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(SOURCE_ROOT.parent))
        violations += find_name_uses(path.read_text(encoding="utf-8"), FORBIDDEN, relative)

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "from langchain_core.globals import set_llm_cache\n",
        "from langchain_core.globals import set_llm_cache as use_cache\n",
        "import langchain_core.globals as g\ng.set_llm_cache(None)\n",
        "set_llm_cache(cache)\n",
        "from langchain_core import globals\nglobals.set_llm_cache(c)\n",
    ],
    ids=["import", "aliased-import", "attribute-call", "bare-call", "module-attribute"],
)
def test_the_scanner_finds_every_way_of_spelling_the_name(source: str) -> None:
    assert find_name_uses(source, FORBIDDEN)


def test_the_scanner_ignores_similar_names() -> None:
    source = "get_llm_cache()\nset_llm_cache_size = 1\nllm_cache = None\n"

    assert find_name_uses(source, FORBIDDEN) == []
