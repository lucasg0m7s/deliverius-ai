"""Finds uses of a name in source code, however it is imported or accessed."""

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NameUse:
    path: str
    line: int


def find_name_uses(source: str, name: str, path: str = "<string>") -> list[NameUse]:
    """Imports, bare names and attributes (`globals.set_llm_cache`) that spell `name`."""
    uses: list[NameUse] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        spelled = (
            (isinstance(node, ast.Name) and node.id == name)
            or (isinstance(node, ast.Attribute) and node.attr == name)
            or (isinstance(node, ast.alias) and node.name.split(".")[-1] == name)
        )
        if spelled:
            uses.append(NameUse(path, getattr(node, "lineno", 0)))
    return uses
