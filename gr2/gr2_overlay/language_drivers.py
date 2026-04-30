"""AST-based language-aware merge drivers for overlay composition."""

from __future__ import annotations

import ast
from pathlib import Path


class PythonCompositionConflict(Exception):
    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"Composition conflict: both base and overlay modify '{symbol}'"
        )
        self.error_code = "composition_conflict"
        self.symbol = symbol


def merge_python_overlay(
    *,
    ancestor: Path,
    current: Path,
    other: Path,
    relative_path: str,
) -> None:
    if not relative_path.endswith(".py"):
        raise ValueError("Python driver only supports .py paths")

    ancestor_tree = ast.parse(ancestor.read_text())
    current_tree = ast.parse(current.read_text())
    other_tree = ast.parse(other.read_text())

    ancestor_imports, ancestor_defs = _split_nodes(ancestor_tree)
    current_imports, current_defs = _split_nodes(current_tree)
    other_imports, other_defs = _split_nodes(other_tree)

    merged_imports = _union_imports(current_imports, other_imports)
    merged_defs = _merge_definitions(ancestor_defs, current_defs, other_defs)

    merged_module = ast.Module(body=merged_imports + merged_defs, type_ignores=[])
    ast.fix_missing_locations(merged_module)
    current.write_text(ast.unparse(merged_module) + "\n")


def _split_nodes(
    tree: ast.Module,
) -> tuple[list[ast.stmt], dict[str, ast.stmt]]:
    imports: list[ast.stmt] = []
    defs: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        else:
            name = _node_name(node)
            if name:
                defs[name] = node
    return imports, defs


def _node_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _union_imports(
    current: list[ast.stmt], other: list[ast.stmt]
) -> list[ast.stmt]:
    seen: set[str] = set()
    result: list[ast.stmt] = []
    for node in current + other:
        key = ast.dump(node)
        if key not in seen:
            seen.add(key)
            result.append(node)
    return result


def _merge_definitions(
    ancestor: dict[str, ast.stmt],
    current: dict[str, ast.stmt],
    other: dict[str, ast.stmt],
) -> list[ast.stmt]:
    all_names = list(dict.fromkeys(list(current.keys()) + list(other.keys())))
    result: list[ast.stmt] = []

    for name in all_names:
        a_node = ancestor.get(name)
        c_node = current.get(name)
        o_node = other.get(name)

        a_dump = ast.dump(a_node) if a_node else None
        c_dump = ast.dump(c_node) if c_node else None
        o_dump = ast.dump(o_node) if o_node else None

        current_changed = c_dump != a_dump
        other_changed = o_dump != a_dump

        if current_changed and other_changed and c_node and o_node:
            if isinstance(o_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result.append(o_node)
            else:
                raise PythonCompositionConflict(name)
        elif o_node and other_changed:
            result.append(o_node)
        elif c_node:
            result.append(c_node)
        elif o_node:
            result.append(o_node)

    return result
