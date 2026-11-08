"""Finds synchronous model calls in source code: the governor only works on the async path."""

import ast
from dataclasses import dataclass

# LangChain's `Runnable` and chat model sync entry points. The async twins are `ainvoke` and so on.
SYNC_ATTRIBUTES = frozenset(
    {"invoke", "stream", "batch", "batch_as_completed", "generate", "predict"}
)


@dataclass(frozen=True, slots=True)
class SyncCall:
    path: str
    line: int
    attribute: str


def find_sync_calls(source: str, path: str = "<string>") -> list[SyncCall]:
    """Every call of the form `something.invoke(...)` (or another sync entry point)."""
    return [
        SyncCall(path, node.lineno, node.func.attr)
        for node in ast.walk(ast.parse(source, filename=path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SYNC_ATTRIBUTES
    ]
