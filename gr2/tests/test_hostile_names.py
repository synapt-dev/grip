"""Every hostile name, fed to each verb that takes a name, must be refused before the verb writes anything (E3).

Rows marked as findings are the names a verb accepts today, or refuses only after writing: strict xfails, so the
shared name validator turns each one into an unexpected pass that must be unmarked.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.hostile_names import HOSTILE_NAMES, classify

RUNNER = make_cli_runner()

SPEC = ('schema_version = 1\nworkspace_name = "m"\n\n[[repos]]\nname = "repo-a"\npath = "repos/repo-a"\nurl = "{url}"\n\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["repo-a"]\n')

# store init --clone, lane create (the lane and --branch), branch and target set check each name with the shared
# GIT-REF profile (gr2/python_cli/names.py) before their first write, so none of them carries a finding now.
FINDINGS = {
    "store-init-clone": set(),
    "lane-create-lane": set(),
    "lane-create-branch": set(),
    "branch": set(),
    "target-set": set(),
    "lane-enter": set(),
    "lane-create-unit": set(),
    "hooks-trust": set(),
    "lane-lease-lane": set(),
    # a check name and a lease actor are labels kept as data, so taking any of them is right, except an empty actor
    "check-run-name": set(),
    "lane-lease-actor": {"empty"},
}
# a nested branch name is a legal branch, so the verb is right to take it
EXPECTED = {("lane-create-branch", "nested"): "accepted", ("branch", "nested"): "accepted",
            ("target-set", "nested"): "accepted"}
EXPECTED.update({("check-run-name", label): "accepted" for label, _ in HOSTILE_NAMES if label != "empty"})
EXPECTED.update({("lane-lease-actor", label): "accepted" for label, _ in HOSTILE_NAMES})
# the finding is that an empty actor is taken; the validator should refuse it
EXPECTED[("lane-lease-actor", "empty")] = "refused-clean"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def _invoke(*args: str) -> tuple[int, str]:
    result = RUNNER.invoke(app, list(args))
    return result.exit_code, result.output


def _bare_with_commit(path: Path) -> Path:
    _git(path.parent, "init", "-q", "--bare", "-b", "main", str(path))
    seed = path.parent / f"{path.name}-seed"
    _git(path.parent, "clone", "-q", str(path), str(seed))
    (seed / "f.txt").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    return path


_TEMPLATE: list[Path] = []


def _template() -> Path:
    # built once per session; each row copies it and points the copy at its own origin, so no row shares one
    if not _TEMPLATE:
        base = Path(tempfile.mkdtemp(prefix="gr2-hostile-"))
        atexit.register(shutil.rmtree, base, True)
        origin = _bare_with_commit(base / "repo-a.git")
        ws = base / "ws"
        (ws / "repos").mkdir(parents=True)
        _git(base, "clone", "-q", str(origin), str(ws / "repos" / "repo-a"))
        (ws / ".grip").mkdir()
        _TEMPLATE.append(base)
    return _TEMPLATE[0]


def _lane_workspace(base: Path) -> Path:
    shutil.copytree(_template(), base, symlinks=True, dirs_exist_ok=True)
    origin, ws = base / "repo-a.git", base / "ws"
    _git(ws / "repos" / "repo-a", "remote", "set-url", "origin", str(origin))
    (ws / ".grip" / "workspace_spec.toml").write_text(SPEC.format(url=str(origin)))
    return ws


def _store_init_clone(tmp_path: Path, name: str):
    url = _bare_with_commit(tmp_path / "alpha.git")
    root = tmp_path / "root"
    return root, lambda: _invoke("store", "init", str(root), "--clone", f"{name}={url}")


def _lane_create_lane(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, lambda: _invoke("lane", "create", str(ws), "atlas", name, "--repos", "repo-a", "--branch", "feat/x")


def _lane_create_branch(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, lambda: _invoke("lane", "create", str(ws), "atlas", "ok", "--repos", "repo-a", "--branch", name)


def _in(cwd: Path, *args: str):
    def run() -> tuple[int, str]:
        old = os.getcwd()
        os.chdir(cwd)
        try:
            return _invoke(*args)
        finally:
            os.chdir(old)
    return run


def _branch(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, _in(ws / "repos" / "repo-a", "branch", name)


def _target_set(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, _in(ws, "target", "set", name, "--repo-path", str(ws / "repos" / "repo-a"))


def _lane_enter(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, lambda: _invoke("lane", "enter", str(ws), "atlas", name)


def _lane_create_unit(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, lambda: _invoke("lane", "create", str(ws), name, "ok", "--repos", "repo-a", "--branch", "feat/x")


def _hooks_trust(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, _in(ws, "hooks", "trust", name, "--workspace-root", str(ws))


def _check_run_name(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return tmp_path, lambda: _invoke("check", "run", str(ws / "repos" / "repo-a"), "--name", name, "--", "true")


def _lane_lease_lane(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    return ws, lambda: _invoke("lane", "lease", "acquire", str(ws), "atlas", name, "--actor", "agent:atlas", "--mode", "edit")


def _lane_lease_actor(tmp_path: Path, name: str):
    ws = _lane_workspace(tmp_path)
    assert _invoke("lane", "create", str(ws), "atlas", "ok", "--repos", "repo-a", "--branch", "feat/x")[0] == 0
    return ws, lambda: _invoke("lane", "lease", "acquire", str(ws), "atlas", "ok", "--actor", name, "--mode", "edit")


VERBS = {
    "store-init-clone": _store_init_clone,
    "lane-create-lane": _lane_create_lane,
    "lane-create-branch": _lane_create_branch,
    "branch": _branch,
    "target-set": _target_set,
    "lane-enter": _lane_enter,
    "lane-create-unit": _lane_create_unit,
    "hooks-trust": _hooks_trust,
    "lane-lease-lane": _lane_lease_lane,
    "check-run-name": _check_run_name,
    "lane-lease-actor": _lane_lease_actor,
}


def _cases():
    for verb in VERBS:
        for label, name in HOSTILE_NAMES:
            marks = [pytest.mark.xfail(strict=True, reason=f"finding: {verb} takes {label}; the shared name validator should refuse it")] \
                if label in FINDINGS[verb] else []
            yield pytest.param(verb, label, name, id=f"{verb}-{label}", marks=marks)


@pytest.mark.parametrize("verb,label,name", list(_cases()))
def test_hostile_name_is_refused_before_any_write(tmp_path, verb, label, name):
    root, run = VERBS[verb](tmp_path, name)
    outcome = classify(root, run)
    expected = EXPECTED.get((verb, label), "refused-clean")
    assert outcome.verdict == expected, (outcome.rc, outcome.changed[:8], outcome.output[-400:])



# the mapping form strips each item's surrounding space as layout, so the space labels are not hostile there
MAPPING_LABELS = ["at", "reflog-at", "lock", "double-dot", "tilde", "colon", "line-separator", "dotdot", "dash"]


@pytest.mark.parametrize("label", MAPPING_LABELS)
def test_lane_create_refuses_a_hostile_branch_given_as_a_repo_mapping(tmp_path, label):
    """`--branch repo=branch` is checked branch by branch, before the lane lock and the cache clone."""
    name = dict(HOSTILE_NAMES)[label]
    ws = _lane_workspace(tmp_path)
    outcome = classify(ws, lambda: _invoke("lane", "create", str(ws), "atlas", "ok", "--repos", "repo-a",
                                           "--branch", f"repo-a={name}"))
    assert outcome.verdict == "refused-clean", (outcome.rc, outcome.changed[:8], outcome.output[-300:])


@pytest.mark.xfail(strict=True, reason="finding: case twins are cloned until they collide")
def test_store_init_refuses_member_names_that_differ_only_by_case(tmp_path):
    # one checkout path on a case-insensitive filesystem, two on a case-sensitive one; either way, refuse first
    first = _bare_with_commit(tmp_path / "a.git")
    second = _bare_with_commit(tmp_path / "b.git")
    root = tmp_path / "root"
    outcome = classify(root, lambda: _invoke("store", "init", str(root), "--clone", f"alpha={first}",
                                             "--clone", f"Alpha={second}"))
    assert outcome.verdict == "refused-clean", (outcome.rc, outcome.changed[:8], outcome.output[-300:])
