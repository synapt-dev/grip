"""`store push --recurse`, `--snapshot` and `--allow-rewind`.

--recurse names what a push does with a member pin its upstream does not cover: `check` (the default) refuses, exit 3;
`none` skips coverage but not the fence; `on-demand` pushes the member from its clone BEFORE the root, and a refused
member push stops the root. --snapshot makes the store commit first. --allow-rewind is parsed and every name it gives
must be a member; nothing else reads it yet.

The uncovered fixture: alpha advances and pushes, the root records it, then alpha's remote is rewound one commit, so the
recorded pin is on no remote while alpha's clone still holds it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_push_member_refs import LIB, _pin, _refuse, _same_repo_store
from tests.test_store_shadow_fence import _hand_pin
from tests.test_store_shadow_s1 import MODES, SHADOW, _advance, _refs, _store


def _uncovered(tmp_path: Path, mode: str) -> tuple[Path, Path, str]:
    root, origin = _store(tmp_path, mode)
    before = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    pin = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    git(tmp_path / "alpha.git", "update-ref", "refs/heads/main", before)
    return root, origin, pin


def test_r1_default_is_recurse_check(tmp_path):
    root, origin = _store(tmp_path, "compat")
    plain = json.loads(gr2(root, "push", "--json").stdout)
    named = json.loads(gr2(root, "push", "--recurse=check", "--json").stdout)
    assert plain["recurse"] == named["recurse"] == "check"
    assert plain["root_commit"] == named["root_commit"] and plain["members"] == named["members"]


@pytest.mark.parametrize("mode", MODES)
def test_r2_check_refuses_an_uncovered_pin_by_name(tmp_path, mode):
    root, origin, _ = _uncovered(tmp_path, mode)
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert "alpha" in result.stderr, result.stderr
    assert _refs(origin) == {}, "origin unmoved"


@pytest.mark.parametrize("mode", MODES)
def test_r3_none_pushes_and_names_the_unchecked(tmp_path, mode):
    root, origin, _ = _uncovered(tmp_path, mode)
    result = gr2(root, "push", "--recurse=none", "--json")
    payload = json.loads(result.stdout)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(origin) == {"refs/heads/main": head, SHADOW: head}
    assert payload["recurse"] == "none" and payload["unchecked"] == ["alpha", "beta"]
    human = gr2(root, "push", "--recurse=none")
    assert "Coverage not checked (--recurse=none): alpha, beta" in human.stdout, human.stdout


@pytest.mark.parametrize("mode", MODES)
def test_r4_none_skips_coverage_not_the_fence(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    remote = _refs(origin)
    _hand_pin(root, "alpha", mode)
    git(root, "commit", "--no-verify", "-m", "hand pin past the hook")
    result = gr2(root, "push", "--recurse=none", check=False)
    assert result.returncode == 4, (result.returncode, result.stderr)
    words = "pin edited by hand" if mode == "full" else "gitlink is not the pin grip.toml records"
    assert words in result.stderr and "alpha" in result.stderr, "the fence's own refusal: " + result.stderr
    assert _refs(origin) == remote


@pytest.mark.parametrize("mode", MODES)
def test_r5_on_demand_pushes_the_member_then_the_root(tmp_path, mode):
    root, origin, pin = _uncovered(tmp_path, mode)
    payload = json.loads(gr2(root, "push", "--recurse=on-demand", "--json").stdout)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(tmp_path / "alpha.git")["refs/heads/main"] == pin
    assert _refs(origin) == {"refs/heads/main": head, SHADOW: head}
    assert {"name": "alpha", "pin": pin, "action": "pushed"} in payload["recursed"]


@pytest.mark.parametrize("mode", MODES)
def test_r6_a_refused_member_push_stops_the_root(tmp_path, mode):
    root, origin, pin = _uncovered(tmp_path, mode)
    _refuse(tmp_path / "alpha.git", "refs/heads/main")
    result = gr2(root, "push", "--recurse=on-demand", check=False)
    assert result.returncode != 0
    assert "alpha" in result.stderr and "refused" in result.stderr, result.stderr
    assert _refs(origin) == {}, "neither origin's branch nor its shadow moved"


def test_r7_a_covered_member_is_not_pushed(tmp_path):
    root, origin, pin = _uncovered(tmp_path, "compat")
    payload = json.loads(gr2(root, "push", "--recurse=on-demand", "--json").stdout)
    actions = {entry["name"]: entry["action"] for entry in payload["recursed"]}
    assert actions == {"alpha": "pushed", "beta": "covered"}


def test_r8_a_member_with_no_clone_is_refused(tmp_path):
    import shutil
    root, origin, _ = _uncovered(tmp_path, "compat")
    shutil.rmtree(root / "alpha")
    result = gr2(root, "push", "--recurse=on-demand", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert "alpha" in result.stderr and "no clone" in result.stderr, result.stderr
    assert _refs(origin) == {}
    assert _refs(tmp_path / "alpha.git")["refs/heads/main"] != git(root, "rev-parse", "HEAD").stdout.strip()


def test_r9_same_repo_member_rides_the_one_atomic_push(tmp_path):
    root, origin = _same_repo_store(tmp_path, "compat")
    lib = _refs(origin)[LIB]
    _refuse(origin, "refs/heads/main")
    result = gr2(root, "push", "--recurse=on-demand", check=False)
    assert result.returncode != 0
    assert _refs(origin)[LIB] == lib, "the member ref did not move apart from the refused root"
    (origin / "hooks" / "pre-receive").unlink()
    payload = json.loads(gr2(root, "push", "--recurse=on-demand", "--json").stdout)
    assert _refs(origin)[LIB] == _pin(root)
    assert {"name": "gamma", "pin": _pin(root), "ref": LIB} in payload["member_refs"]


@pytest.mark.parametrize("mode", MODES)
def test_r10_snapshot_commits_then_pushes(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    old = git(root, "rev-parse", "HEAD").stdout.strip()
    _advance(root, "alpha")
    gr2(root, "push")
    assert _refs(origin)["refs/heads/main"] == old, "control: without --snapshot origin gets the old tip"
    payload = json.loads(gr2(root, "push", "--snapshot", "--json").stdout)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert head != old and payload["snapshot"] == head
    assert _refs(origin)["refs/heads/main"] == head and _refs(origin)[SHADOW] == head
    pin = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    assert pin in git(root, "show", f"{head}:grip.toml").stdout


def test_r11_snapshot_with_nothing_to_record(tmp_path):
    root, origin = _store(tmp_path, "compat")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    payload = json.loads(gr2(root, "push", "--snapshot", "--json").stdout)
    assert payload["snapshot"] is None
    assert git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert _refs(origin)["refs/heads/main"] == head


def test_r12_refused_snapshot_push_keeps_the_commit(tmp_path):
    root, origin = _store(tmp_path, "compat")
    _advance(root, "alpha")
    _refuse(origin, "refs/heads/main")
    result = gr2(root, "push", "--snapshot", check=False)
    assert result.returncode != 0
    made = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(origin) == {}
    (origin / "hooks" / "pre-receive").unlink()
    gr2(root, "push")
    assert git(root, "rev-parse", "HEAD").stdout.strip() == made, "no second commit"
    assert _refs(origin)["refs/heads/main"] == made


def test_r13_allow_rewind_refuses_an_unknown_member(tmp_path):
    root, origin = _store(tmp_path, "compat")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    _advance(root, "alpha")
    result = gr2(root, "push", "--snapshot", "--allow-rewind", "alpha", "--allow-rewind", "nosuch", check=False)
    assert result.returncode == 2, (result.returncode, result.stderr)
    assert "nosuch" in result.stderr, result.stderr
    assert git(root, "rev-parse", "HEAD").stdout.strip() == head, "no snapshot commit"
    assert _refs(origin) == {}
    ok = json.loads(gr2(root, "push", "--allow-rewind", "alpha", "--json").stdout)
    assert ok["allow_rewind"] == ["alpha"]
