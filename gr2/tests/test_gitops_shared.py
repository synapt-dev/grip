"""Every git call gr2 makes goes through gitops: one place for the no-auto-maintenance flags and the bound.

The first rows run each shared verb against a fake `git` on PATH that records its argv. The last row is the guard:
no module outside gitops may spawn git itself, so a new private copy is a failing test, not a review comment.
"""
from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops

PACKAGE = Path(gitops.__file__).resolve().parent.parent


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\necho ok\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.mark.parametrize("call", [
    lambda d: gitops.git(d, "status"),
    lambda d: gitops.run(d, "status"),
    lambda d: gitops.out(d, "status"),
    lambda d: gitops.out_bytes(d, "status", input=b"x"),
    lambda d: gitops.run_argv(["git", "status"], cwd=d),
])
def test_every_shared_verb_disables_auto_maintenance(recorded, tmp_path, call):
    call(tmp_path)
    line = recorded.read_text().splitlines()[-1]
    assert line.startswith("-c maintenance.auto=false -c gc.auto=0 "), line


def test_an_expiry_is_rc_124_unless_the_caller_asks_for_the_raise(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert gitops.run(tmp_path, "status", timeout=0.2).returncode == 124
    with pytest.raises(subprocess.TimeoutExpired):
        gitops.run(tmp_path, "status", timeout=0.2, raise_timeout=True)


def test_a_failure_raises_the_callers_error_type(tmp_path):
    class Refused(RuntimeError):
        pass
    with pytest.raises(Refused, match="git rev-parse"):
        gitops.out(tmp_path / "missing", "rev-parse", "HEAD", error=Refused)
    with pytest.raises(subprocess.CalledProcessError):
        gitops.check(gitops.run(tmp_path / "missing", "rev-parse", "HEAD"))


# ---- the guard: subprocess spawns in python_cli/ and overlay/, classified by their argv -------------------------
# A tripwire for the shapes code here is written in, not a proof over all Python: it reads spawns through
# `import subprocess [as x]`, `from subprocess import run [as y]` and an alias assigned to one of those, with a
# positional or `args=` argv. An argv is git only when it provably is: a List/Tuple literal at the call, or a name
# whose every binding in its scope is the same literal and whose first word nothing rewrites. Anything else (a
# parameter, disagreeing bindings, an item or slice write, an in-place method that can change the first word) is
# UNRESOLVED, and an unresolved spawn is allowed only at a named seam that runs a user's or a tool's command. A
# literal whose basename is `git` (`/usr/bin/git`) is git. prototypes/ is outside this guard and this change.
SPAWNS = {"run", "Popen", "check_output", "check_call", "call"}
_HEAD_WRITERS = {"insert", "clear", "pop", "remove", "reverse", "sort", "__setitem__"}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(scope):
    """The nodes of a scope's own body, not of the functions or classes nested in it."""
    nodes, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        nodes.append(n)
        if not isinstance(n, (*_FUNCS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return nodes


def _params(fn):
    a = fn.args
    return {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x is not None}


def _kind_of_literal(node):
    if not (isinstance(node, (ast.List, ast.Tuple)) and node.elts):
        return "unresolved"
    h = node.elts[0]
    if isinstance(h, ast.Constant) and isinstance(h.value, str):
        return "git" if h.value.rsplit("/", 1)[-1] == "git" else "other"
    return "unresolved"


class _Module:
    def __init__(self, tree):
        self.tree = tree
        self.scopes = [(tree, _own_nodes(tree))] + [(n, _own_nodes(n)) for n in ast.walk(tree) if isinstance(n, _FUNCS)]
        self.subprocess_mods, self.spawn_imports = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                self.subprocess_mods |= {a.asname or a.name for a in n.names if a.name == "subprocess"}
            elif isinstance(n, ast.ImportFrom) and n.module == "subprocess":
                self.spawn_imports |= {a.asname or a.name for a in n.names if a.name in SPAWNS}

    def chain(self, node):
        """The scopes a name at `node` resolves through: its own function first, then the module."""
        own = next((s for s in self.scopes[1:] if any(n is node for n in s[1])), self.scopes[0])
        return [own] if own is self.scopes[0] else [own, self.scopes[0]]

    def bindings(self, name, node):
        """(values bound to `name` in the first scope that binds it, rewritten?), or None for a parameter/unbound."""
        for scope, nodes in self.chain(node):
            if isinstance(scope, _FUNCS) and name in _params(scope):
                return None
            vals = [n.value for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                    and any(isinstance(t, ast.Name) and t.id == name for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
            rebound = any(isinstance(n, (ast.AugAssign, ast.For, ast.With, ast.NamedExpr)) and name in
                          {t.id for t in ast.walk(n.target if hasattr(n, "target") else n) if isinstance(t, ast.Name)
                           and isinstance(t.ctx, ast.Store)} for n in nodes if isinstance(n, (ast.AugAssign, ast.For, ast.NamedExpr)))
            rewritten = rebound or any(
                (isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and isinstance(n.value, ast.Name)
                 and n.value.id == name)
                or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _HEAD_WRITERS
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == name)
                for n in nodes)
            if vals or rewritten:
                return vals, rewritten
        return None

    def is_spawn(self, f, node):
        if isinstance(f, ast.Attribute):
            return f.attr in SPAWNS and isinstance(f.value, ast.Name) and f.value.id in self.subprocess_mods
        if not isinstance(f, ast.Name):
            return False
        b = self.bindings(f.id, node)
        if b is None:
            return f.id in self.spawn_imports and not any(
                isinstance(s, _FUNCS) and f.id in _params(s) for s, _ in self.chain(node))
        vals, _ = b
        return any(self.is_spawn(v, node) for v in vals if isinstance(v, (ast.Attribute, ast.Name)) and v is not f)

    def argv_kind(self, argv, node):
        if argv is None:
            return "unresolved"
        if not isinstance(argv, ast.Name):
            return _kind_of_literal(argv)
        b = self.bindings(argv.id, node)
        if b is None:
            return "unresolved"
        vals, rewritten = b
        kinds = {_kind_of_literal(v) for v in vals}  # every binding's own first word, so binding order never matters
        return "unresolved" if rewritten or len(kinds) != 1 else kinds.pop()


def spawns(path: Path):
    """Yield (function name, line, kind) for every subprocess spawn in the file."""
    m = _Module(ast.parse(path.read_text()))
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and m.is_spawn(node.func, node)):
            continue
        argv = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
        scope = m.chain(node)[0][0]
        yield getattr(scope, "name", "<module>"), node.lineno, m.argv_kind(argv, node)


class _Src:
    def __init__(self, text):
        self.text = text

    def read_text(self):
        return self.text


def _spawns_in_source(src: str):
    return [k for _, _, k in spawns(_Src(src))]


# Where git itself is started: the one shared spawn, and version.py's resolved-binary read (a stranger's machine
# with no git degrades to a less specific version string instead of failing).
GIT_SPAWNS = {("python_cli/gitops.py", "_spawn"), ("python_cli/version.py", "_clone_short_sha")}
# Where a spawn's argv is not a literal because it IS someone else's command: a user's check, test or install
# command, a venv or pip step, a hook, a plugin, a launched agent, or the hosting platform's CLI.
USER_COMMAND_SEAMS = {
    ("python_cli/check_records.py", "run_check"),
    ("python_cli/env_exec.py", "import_origin"), ("python_cli/env_exec.py", "probe_venv"),
    ("python_cli/execops.py", "_exec_one"),
    ("python_cli/git_review.py", "_run_pytest"), ("python_cli/git_review.py", "_run_non_pytest"),
    ("python_cli/grip.py", "_run_policy_hook"), ("python_cli/grip.py", "run_review_checks"),
    # `gr` runs the half it chose (Windows only; elsewhere it execs), and asks the `gr` first on PATH its version.
    ("python_cli/gr_resolver.py", "_exec"), ("python_cli/gr_shadow.py", "_gr1_version"),
    ("python_cli/hooks.py", "run_lifecycle_stage"),
    ("python_cli/lane_plugins.py", "call"),
    ("python_cli/launch_exec.py", "launch_unit"), ("python_cli/launch_exec.py", "launch_team"),
    ("python_cli/launch_exec.py", "_invoke"),
    ("python_cli/open_gr_review.py", "provision_lane_venv"),
    ("python_cli/open_gr_review.py", "record_review_verification"),
    ("python_cli/platform.py", "_run_json"), ("python_cli/platform.py", "merge_pr"),
    ("python_cli/platform.py", "create_pr"), ("python_cli/platform.py", "edit_pr_body"),
    ("python_cli/review.py", "run_in_review_lane"),
    ("python_cli/review_run.py", "resolve_import_file"), ("python_cli/review_run.py", "_create_lane_venv"),
    ("python_cli/review_run.py", "_run_member_steps"), ("python_cli/review_run.py", "run_test_command_in_lane"),
}


def _package_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("prototypes/") or "/tests/" in f"/{rel}":
            continue
        yield rel, path


def test_git_is_spawned_only_by_the_shared_spawn():
    found = [f"{rel}:{line} {fn}" for rel, path in _package_files() for fn, line, kind in spawns(path)
             if kind == "git" and (rel, fn) not in GIT_SPAWNS]
    assert not found, "run git through gitops (run, out, out_bytes, run_argv, clone):\n" + "\n".join(found)


def test_a_spawn_the_guard_cannot_read_is_a_named_command_seam():
    found = [f"{rel}:{line} {fn}" for rel, path in _package_files() for fn, line, kind in spawns(path)
             if kind == "unresolved" and (rel, fn) not in USER_COMMAND_SEAMS | GIT_SPAWNS]
    assert not found, ("a spawn whose argv the guard cannot resolve: give it a literal argv, or, if it runs a "
                       "user's or a tool's command, name its function in USER_COMMAND_SEAMS:\n" + "\n".join(found))


@pytest.mark.parametrize("shape,src", [
    ("list", 'import subprocess\ndef f():\n    subprocess.run(["git", "status"])\n'),
    ("tuple", 'import subprocess\ndef f():\n    subprocess.run(("git", "status"))\n'),
    ("assigned argv", 'import subprocess\ndef f():\n    cmd = ["git", "status"]\n    cmd.append("-s")\n    subprocess.run(cmd)\n'),
    ("args keyword", 'import subprocess\ndef f():\n    subprocess.run(args=["git", "status"])\n'),
    ("module alias", 'import subprocess as sp\ndef f():\n    sp.check_output(["git", "status"])\n'),
    ("function alias", 'from subprocess import run as r\ndef f():\n    r(["git", "status"])\n'),
    ("Popen", 'from subprocess import Popen\ndef f():\n    Popen(["git", "status"])\n'),
    ("assigned spawn alias", 'import subprocess\ndef f():\n    launch = subprocess.run\n    launch(["git", "status"])\n'),
    ("module-level spawn alias", 'import subprocess\nlaunch = subprocess.run\ndef f():\n    launch(["git", "status"])\n'),
    ("absolute git", 'import subprocess\ndef f():\n    subprocess.run(["/usr/bin/git", "status"])\n'),
    ("module argv", 'import subprocess\ncmd = ["git", "status"]\ndef f():\n    subprocess.run(cmd)\ndef unrelated():\n    cmd = ["echo", "later"]\n'),
])
def test_the_guard_sees_git(shape, src):
    assert _spawns_in_source(src) == ["git"], shape


@pytest.mark.parametrize("shape,src", [
    ("later reassignment", 'import subprocess\ndef f():\n    cmd = ["git", "status"]\n    subprocess.run(cmd)\n    cmd = ["echo", "later"]\n'),
    ("disagreeing bindings", 'import subprocess\ndef f(x):\n    c = ["echo"]\n    if x:\n        c = ["git"]\n    subprocess.run(c)\n'),
    ("parameter shadows a global", 'import subprocess\ncmd = ["echo"]\ndef f(cmd):\n    subprocess.run(cmd)\n'),
    ("first word rewritten", 'import subprocess\ndef f():\n    cmd = ["echo"]\n    cmd[0] = "git"\n    subprocess.run(cmd)\n'),
    ("first word inserted", 'import subprocess\ndef f():\n    cmd = ["status"]\n    cmd.insert(0, "git")\n    subprocess.run(cmd)\n'),
    ("argv from a call", 'import subprocess\ndef f(x):\n    subprocess.run(build(x))\n'),
])
def test_an_argv_the_guard_cannot_prove_is_unresolved(shape, src):
    assert _spawns_in_source(src) == ["unresolved"], shape


@pytest.mark.parametrize("shape,src,expected", [
    ("other literal", 'import subprocess\nsubprocess.run(["gh", "pr"])\n', ["other"]),
    ("other absolute", 'import subprocess\nsubprocess.run(["/usr/bin/gitk"])\n', ["other"]),
    ("a non-spawn alias", 'import subprocess\ndef f():\n    launch = print\n    launch(["git", "status"])\n', []),
    ("a sibling's spawn alias", 'import subprocess\ndef g():\n    launch = subprocess.run\n'
                                'def f():\n    launch = print\n    launch(["git", "status"])\n', []),
    ("a parameter named like a spawn", 'from subprocess import run\ndef f(run):\n    run(["git", "status"])\n', []),
])
def test_the_guard_does_not_call_a_non_git_spawn_git(shape, src, expected):
    assert _spawns_in_source(src) == expected, shape


@pytest.mark.parametrize("argv", [
    ["git", "clone", "src", "dst"],
    ["git", "-C", "repo", "clone", "src", "dst"],
    ["git", "-c", "k=v", "--git-dir", "d", "clone", "src", "dst"],
    ["git", "--git-dir=d", "clone", "src", "dst"],
    ["git", "--config-env", "core.sshCommand=SSH_CMD", "clone", "src", "dst"],
    ["git", "--attr-source", "HEAD", "clone", "src", "dst"],
    ["git", "--config-env=core.sshCommand=SSH_CMD", "clone", "src", "dst"],
])
def test_a_clone_is_unbounded_whichever_helper_runs_it(monkeypatch, tmp_path, argv):
    """Clones stay unbounded: their duration scales with the repository. The spawn decides, not the caller."""
    seen = []
    monkeypatch.setattr(gitops.subprocess, "run", lambda a, **kw: seen.append((a, kw.get("timeout")))
                        or subprocess.CompletedProcess(a, 0, "", ""))
    gitops.run_argv(argv)
    gitops.run(tmp_path, *argv[1:])
    gitops.clone("src", "dst")
    gitops.run_argv(["git", "-C", "clone", "status"])  # a directory NAMED clone is not the clone command
    gitops.run(tmp_path, "status")
    assert [t for _, t in seen] == [None, None, None, gitops.DEFAULT_TIMEOUT, gitops.DEFAULT_TIMEOUT]


def _one_member_merge(monkeypatch, tmp_path, merge_tree):
    from gr2.python_cli import merge_gate
    rid, head, base = "a" * 40, "b" * 40, "c" * 40
    view = {"id": "gr:" + rid, "members": [{"key": "one", "path": "member", "remote": "r", "head": head, "base": base}]}
    monkeypatch.setattr(merge_gate.grip, "verify_review_commit", lambda *a: {"tree_matches": True})
    monkeypatch.setattr(merge_gate.grip, "show_review_commit", lambda *a: view)
    monkeypatch.setattr(merge_gate, "_toplevel", lambda p: p.resolve())
    monkeypatch.setattr(merge_gate, "_store_inside", lambda *a: True)
    monkeypatch.setattr(merge_gate, "_remote_tip", lambda p, r, b: base if b == "main" else head)
    monkeypatch.setattr(merge_gate, "_advertised", lambda *a: rid)
    monkeypatch.setattr(merge_gate, "_merged_at", lambda *a: None)
    monkeypatch.setattr(merge_gate, "_git", lambda *a, **k: base)
    monkeypatch.setattr(merge_gate.check_records, "read_remote_check",
                        lambda *a, **k: {"status": "pass", "record_id": "d" * 40, "reason": "ok"})
    monkeypatch.setattr(merge_gate, "_state", lambda *a: ("unmerged", None))
    calls = []

    def spawn(argv, **kw):
        calls.append(argv)
        assert "merge-tree" in argv
        return merge_tree(argv)
    monkeypatch.setattr(gitops.subprocess, "run", spawn)
    code, receipt = merge_gate.review_merge(tmp_path, rid, into="main", feature="feat")
    return code, receipt["members"][0]["refused"], calls


def test_a_merge_tree_timeout_is_a_build_failure_not_a_conflict(monkeypatch, tmp_path):
    def expire(argv):
        raise subprocess.TimeoutExpired(argv, 60)
    code, refused, calls = _one_member_merge(monkeypatch, tmp_path, expire)
    assert code == 3 and len(calls) == 1 and refused.startswith("merge_build_failed:"), refused


def test_a_real_merge_tree_conflict_is_still_a_conflict(monkeypatch, tmp_path):
    code, refused, calls = _one_member_merge(
        monkeypatch, tmp_path, lambda argv: subprocess.CompletedProcess(argv, 1, "", "CONFLICT"))
    assert code == 3 and len(calls) == 1 and refused == "merge_conflict", refused
