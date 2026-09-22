#!/usr/bin/env python3
"""Enumerate every git clone/fetch call site in gr2 and classify each.

Built for the 2026-09-22 gr2 dogfood class sweep, so the table is produced by a
run rather than assembled by hand.

THREE INSTRUMENT FAULTS this script had and now does not, each found by running
it and disbelieving the table:

  1. Reading only the STRING LITERALS IN A CALL's argv missed
     `gitops.clone_repo` -- the function the finding is named after -- because
     it builds its argv in a local variable first:
         command = ["git", "clone"]
         subprocess.run(command, ...)
  2. Adding a coarse "does this function mention clone" fallback then
     OVER-MATCHED: it flagged every process call in `ensure_repo_cache`,
     including its `remote set-url` and `remote update` calls, because the word
     `clone` appears somewhere in the same body.
  3. The fetch sites vanished entirely, because the gr2 git helpers (`git`,
     `_git`, `gitops.git`) are not `subprocess.run` and were not in RUNNERS.

The fix for 1 and 2 together is to RESOLVE THE VARIABLE: map local names
assigned a list literal to the verbs in that literal, then report a runner call
only when one of its arguments is such a name. One row per real call site.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "python_cli"

REWRITE = "_effective_remote_url"
VERBS = {"clone", "fetch"}
# `run`/`Popen` etc. are subprocess; the rest are gr2's OWN private git
# wrappers. There are EIGHT of them across seven modules with six signatures,
# found by following subprocess rather than by guessing names -- and enumeration
# by guessed name missed `merge_verification._run_git` entirely:
#   gitops.git(cwd,*a,timeout=None) | grip._grip_git | grip._run | grip._lg
#   merge_verification._run_git | review_ephemeral._git | review_run._git
#   git_review._git | review_rebind._git
RUNNERS = {"run", "check_call", "check_output", "Popen", "call",
           "git", "_git", "_run_git", "_grip_git", "_run", "_lg"}


def literal_verbs(node: ast.AST) -> set[str]:
    """clone/fetch among the string literals in an expression."""
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)} & VERBS


def callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        base = f.value.id + "." if isinstance(f.value, ast.Name) else ""
        return base + f.attr
    return f.id if isinstance(f, ast.Name) else "?"


def last_name(node: ast.Call) -> str:
    return callee_name(node).split(".")[-1]


def arg_verbs(node: ast.Call) -> set[str]:
    """Verbs in the args of a call: literals, plus literals of any list arg."""
    out: set[str] = set()
    for arg in list(node.args) + [k.value for k in node.keywords if k.arg is None]:
        out |= literal_verbs(arg)
    return out


def enclosing(tree: ast.AST) -> dict[int, ast.FunctionDef]:
    out: dict[int, ast.FunctionDef] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for sub in ast.walk(fn):
            ln = getattr(sub, "lineno", None)
            if ln is None:
                continue
            if ln not in out or fn.lineno > out[ln].lineno:
                out[ln] = fn
    return out


def main() -> int:
    rows: set[tuple] = set()
    unclassified: list[str] = []

    for path in sorted(PKG.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        loc = enclosing(tree)
        rel = str(path.relative_to(ROOT))
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

        for fn in fns:
            body_nodes = list(ast.walk(fn))
            params = [a.arg for a in (fn.args.args + fn.args.kwonlyargs)]
            local_params = [p for p in params if any(t in p for t in ("local", "source", "seed"))]
            body_src = ast.get_source_segment(src, fn) or ""
            rewrite = "yes" if REWRITE in body_src else "no"

            # local name -> verbs, from assignments of list/tuple literals
            varverbs: dict[str, set[str]] = {}
            for stmt in body_nodes:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.List, ast.Tuple)):
                    v = literal_verbs(stmt.value)
                    if v:
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                varverbs[tgt.id] = v
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, (ast.List, ast.Tuple)):
                    v = literal_verbs(stmt.value)
                    if v and isinstance(stmt.target, ast.Name):
                        varverbs[stmt.target.id] = v

            for node in body_nodes:
                if not isinstance(node, ast.Call) or last_name(node) not in RUNNERS:
                    continue
                verbs = arg_verbs(node)
                how = "argv=literal"
                if not verbs:
                    # resolve a name argument against the variable map
                    for arg in list(node.args):
                        if isinstance(arg, ast.Name) and arg.id in varverbs:
                            verbs = varverbs[arg.id]
                            how = f"argv=var({arg.id})"
                            break
                if not verbs:
                    continue
                verb = "clone" if "clone" in verbs else "fetch"
                kwargs = {k.arg for k in node.keywords if k.arg}
                timeout = ("yes" if "timeout" in kwargs
                           else ("fn-default" if "timeout=" in body_src else "no"))
                rows.add((rel, node.lineno, fn.name, verb, timeout,
                          "yes" if local_params else "no", rewrite,
                          ",".join(local_params) or "-", how))

            # anything that names a verb but produced no row is worth knowing about
            if literal_verbs(ast.parse("") if False else fn) or (VERBS & {
                    n.value for n in body_nodes
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}):
                pass

    print(f"{'file:line':34} {'function':30} {'verb':6} {'timeout':11} {'localseed':10} "
          f"{'rewrite':8} {'how':18} param")
    print("-" * 155)
    for r in sorted(rows):
        print(f"{r[0]+':'+str(r[1]):34} {r[2]:30} {r[3]:6} {r[4]:11} {r[5]:10} "
              f"{r[6]:8} {r[8]:18} {r[7]}")
    print()
    s = sorted(rows)
    print(f"call sites: {len(s)}   clone: {sum(1 for r in s if r[3]=='clone')}   "
          f"fetch: {sum(1 for r in s if r[3]=='fetch')}   "
          f"pass timeout=: {sum(1 for r in s if r[4]=='yes')}   "
          f"offer local-seed: {sum(1 for r in s if r[5]=='yes')}   "
          f"ssh-rewrite: {sum(1 for r in s if r[6]=='yes')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
