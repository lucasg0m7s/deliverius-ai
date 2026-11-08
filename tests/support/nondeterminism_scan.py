"""Finds sources of nondeterminism in code that must be reproducible from a seed alone."""

import ast
from dataclasses import dataclass

# Not random from the seed: the clock, the global generator, random UUIDs, OS entropy.
FORBIDDEN_CALL_ATTRIBUTES = {
    "datetime": {"now", "utcnow", "today"},
    "date": {"today"},
    "time": {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"},
    "os": {"urandom"},
    "Faker": {"seed"},
}
FORBIDDEN_NAMES = frozenset({"uuid7", "uuid4", "uuid1", "new_id"})
FORBIDDEN_MODULES = frozenset({"secrets"})
# `random.Random(seed)` is the one allowed door into the module.
ALLOWED_RANDOM_NAMES = frozenset({"Random"})


@dataclass(frozen=True, slots=True)
class Nondeterminism:
    path: str
    line: int
    what: str


def find_nondeterminism(source: str, path: str = "<string>") -> list[Nondeterminism]:
    found: list[Nondeterminism] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        what = _describe(node)
        if what is not None:
            found.append(Nondeterminism(path, getattr(node, "lineno", 0), what))
    return found


def _describe(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return _describe_attribute(node)
    if isinstance(node, ast.Name):
        return node.id if node.id in FORBIDDEN_NAMES else None
    if isinstance(node, ast.ImportFrom):
        return _describe_import_from(node)
    if isinstance(node, ast.Import):
        return next((a.name for a in node.names if a.name in FORBIDDEN_MODULES), None)
    return None


def _describe_attribute(node: ast.Attribute) -> str | None:
    owner = _owner_name(node.value)
    if owner == "random" and node.attr not in ALLOWED_RANDOM_NAMES:
        return f"random.{node.attr}"
    if owner is not None and node.attr in FORBIDDEN_CALL_ATTRIBUTES.get(owner, ()):
        return f"{owner}.{node.attr}"
    return node.attr if node.attr in FORBIDDEN_NAMES else None


def _owner_name(node: ast.expr) -> str | None:
    """`datetime` in `datetime.now` and in `datetime.datetime.now` alike."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _describe_import_from(node: ast.ImportFrom) -> str | None:
    module = node.module or ""
    names = {alias.name for alias in node.names}
    if module in FORBIDDEN_MODULES:
        return module
    if module == "random" and names - ALLOWED_RANDOM_NAMES:
        return f"from random import {', '.join(sorted(names - ALLOWED_RANDOM_NAMES))}"
    banned = names & FORBIDDEN_NAMES
    if banned:
        return f"import {', '.join(sorted(banned))}"
    return next(
        (
            f"from {module} import {name}"
            for name in names
            if name in FORBIDDEN_CALL_ATTRIBUTES.get(module, ())
        ),
        None,
    )
