"""No model is called synchronously: a sync `invoke` swallows the governor's refusal (task 5.8)."""

from pathlib import Path

import pytest
from support.sync_call_scan import SYNC_ATTRIBUTES, SyncCall, find_sync_calls

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "deliverius"


def source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


@pytest.mark.parametrize("package", ["datagen", "cli"])
def test_the_scan_covers_the_development_tools(package: str) -> None:
    assert SOURCE_ROOT / package / "__init__.py" in source_files()


def test_no_source_file_calls_a_model_synchronously() -> None:
    violations: list[SyncCall] = []
    for path in source_files():
        relative = str(path.relative_to(SOURCE_ROOT.parent))
        violations += find_sync_calls(path.read_text(encoding="utf-8"), relative)

    assert violations == [], "use the async variant (ainvoke, astream, abatch, ...)"


@pytest.mark.parametrize("attribute", sorted(SYNC_ATTRIBUTES))
def test_the_scanner_flags_every_synchronous_entry_point(attribute: str) -> None:
    source = f"def answer(model, messages):\n    return model.{attribute}(messages)\n"

    assert find_sync_calls(source, "module.py") == [SyncCall("module.py", 2, attribute)]


def test_the_scanner_flags_a_call_in_the_middle_of_a_chain() -> None:
    source = "reply = (prompt | model).invoke({'question': text}).content\n"

    assert [call.attribute for call in find_sync_calls(source)] == ["invoke"]


def test_the_scanner_lets_the_async_variants_pass() -> None:
    source = (
        "async def answer(model, messages):\n"
        "    reply = await model.ainvoke(messages)\n"
        "    async for chunk in model.astream(messages):\n"
        "        print(chunk)\n"
        "    return await model.abatch([messages])\n"
    )

    assert find_sync_calls(source) == []


def test_the_scanner_ignores_names_that_are_not_attribute_calls() -> None:
    source = "def invoke(x):\n    return x\n\ninvoke(1)\nresult = obj.invoke\n"

    assert find_sync_calls(source) == []
