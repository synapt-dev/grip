"""Every ancestry question gr2 acts on goes through gitops.is_ancestor / gitops.ancestry_proc, which ignore replace
refs. A literal "--is-ancestor" outside gitops.py is a site that reads replaced history (and often rc 128 as "no").

This row counts them by AST, so reverting a migrated site back to a raw `gitops.run(..., "--is-ancestor", ...)`
goes red. The allowlist below is empty; a new literal site goes through gitops.is_ancestor instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

from gr2.python_cli import gitops

#: The gr2 package dir (gr2/gr2), found through a module so a namespace-package import can't blank it.
PACKAGE = Path(gitops.__file__).resolve().parents[1]

#: Literal "--is-ancestor" strings still allowed outside gitops.py, by path under the gr2 package: none. The
#: approvals.py and merge_gate.py sites moved onto gitops.is_ancestor with the review ancestry reads.
ALLOWED: dict[str, int] = {}


def _literal_is_ancestor_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "gitops.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Every string constant, not only direct call arguments: an argv built as a list
        # (`subprocess.run(["git", "merge-base", "--is-ancestor", ...])`) escapes a call-args-only count.
        n = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "--is-ancestor")
        if n:
            counts[path.relative_to(PACKAGE).as_posix()] = n
    return counts


def test_only_the_owed_sites_pass_is_ancestor_literally():
    assert _literal_is_ancestor_counts() == ALLOWED


def test_the_walk_sees_the_package():
    # The guard is void if the walk reads an empty or wrong tree: gitops.py must exist where it looks.
    assert (PACKAGE / "python_cli" / "gitops.py").is_file()
    assert len(list(PACKAGE.rglob("*.py"))) > 20
