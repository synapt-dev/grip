"""Every merge-tree and merge-base the merge gate acts on reads the real history (gitops.real_history_run): no
replace refs, no grafts, no commit-graph. A plain `gitops.run(repo, "merge-base", ...)` or `_git(repo, "merge-tree",
...)` without `history=True` in merge_gate.py would compute a base or a merge from replaced history.

This row walks merge_gate.py by AST and checks each literal "merge-tree" / "merge-base" string is a positional
argument of `gitops.real_history_run(...)` or of `_git(..., history=True)`. Any other site (a plain run, a
subprocess argv list, `_git` without the flag) goes red and is named by line. The allowlist below is empty.
"""
from __future__ import annotations

import ast
from pathlib import Path

from gr2.python_cli import merge_gate

#: merge_gate.py, found through the module so the scan reads the file the tests import.
SOURCE = Path(merge_gate.__file__).resolve()

#: The git subcommands whose answer depends on history.
HISTORY_VERBS = ("merge-tree", "merge-base")

#: "<verb> line <n>" sites still allowed to run outside real_history_run: none.
ALLOWED: list[str] = []


def _routes_real_history(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "real_history_run":
        return isinstance(func.value, ast.Name) and func.value.id == "gitops"
    if isinstance(func, ast.Name) and func.id == "_git":
        return any(kw.arg == "history" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                   for kw in call.keywords)
    return False


def _history_sites() -> tuple[dict[str, int], list[str]]:
    """(count per verb, sites not routed through real_history_run as "<verb> line <n>")."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    counts = dict.fromkeys(HISTORY_VERBS, 0)
    bad: list[str] = []
    # Every string constant, not only call arguments: an argv list (`subprocess.run(["git", "merge-base", ...])`)
    # or a tuple spliced in with * would escape a call-args-only scan.
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value in HISTORY_VERBS):
            continue
        counts[node.value] += 1
        up = parent.get(node)
        if not (isinstance(up, ast.Call) and node in up.args and _routes_real_history(up)):
            bad.append(f"{node.value} line {node.lineno}")
    return counts, bad


def test_merge_gate_runs_merge_tree_and_merge_base_only_in_the_real_history():
    counts, bad = _history_sites()
    print(f"merge_gate sites scanned: {counts}")
    assert bad == ALLOWED, f"{SOURCE.name}: merge-tree/merge-base outside real_history_run at {bad}"


def test_the_scan_sees_the_gate_sites():
    # The guard is void if the scan reads the wrong file or no site: the gate has at least one of each verb.
    counts, _ = _history_sites()
    assert SOURCE.name == "merge_gate.py" and SOURCE.is_file()
    assert all(n >= 1 for n in counts.values()), counts
