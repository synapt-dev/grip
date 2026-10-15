"""`store push` leases 1 and 3 (plan section 5): freshness against this clone's tracking refs, then origin's value on
the tip's first-parent line, then a push leased on the tracked values, atomic.

origin's shadow is tracked by a fetch line the verb adds to the root's origin when it is missing (and says so on
stderr); another writer is played by moving origin's refs directly, so no second clone is needed.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import MODES, SHADOW, _advance, _refs, _refuse_shadow, _store

MAIN = "refs/heads/main"
LINE = "+refs/dev.synapt.grip/__branches__/*:refs/remotes/origin/dev.synapt.grip/__branches__/*"
TRACKED = "refs/remotes/origin/dev.synapt.grip/__branches__/main/SHADOW"


def _pushed(tmp_path: Path, mode: str) -> tuple[Path, Path, str]:
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    return root, origin, git(root, "rev-parse", "HEAD").stdout.strip()


def _commit(root: Path, *parents: str, message: str = "other writer") -> str:
    args = [arg for parent in parents for arg in ("-p", parent)]
    return git(root, "commit-tree", "HEAD^{tree}", *args, "-m", message).stdout.strip()


def _land(root: Path, origin: Path, sha: str, *refs: str) -> None:
    """Another writer: origin gets `sha` and each of `refs` names it; this clone's tracking refs do not move."""
    git(root, "push", "-q", "--no-verify", "origin", f"{sha}:refs/tmp/other")
    for ref in refs:
        git(origin, "update-ref", ref, sha)
    git(origin, "update-ref", "-d", "refs/tmp/other")


def _fetch_lines(root: Path) -> list[str]:
    return git(root, "config", "--get-all", "remote.origin.fetch").stdout.splitlines()


@pytest.mark.parametrize("mode", MODES)
def test_l1_control_descendant_pushes_and_tracks_the_shadow(tmp_path, mode):
    root, origin, _ = _pushed(tmp_path, mode)
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    gr2(root, "push")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(origin) == {MAIN: head, SHADOW: head}
    assert git(root, "rev-parse", TRACKED).stdout.strip() == head
    assert _fetch_lines(root) == ["+refs/heads/*:refs/remotes/origin/*", LINE]


@pytest.mark.parametrize("mode", MODES)
def test_l1a_behind_is_refused_by_name(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    ahead = _commit(root, first)
    _land(root, origin, ahead, MAIN, SHADOW)
    git(root, "fetch", "-q", "origin")
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"origin's {MAIN} is {ahead}, this push's tip is {first}; behind" in result.stderr, result.stderr
    assert _refs(origin) == {MAIN: ahead, SHADOW: ahead}


@pytest.mark.parametrize("mode", MODES)
def test_l1b_rewrite_is_refused_by_name(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    other = _commit(root)
    _land(root, origin, other, MAIN, SHADOW)
    git(root, "fetch", "-q", "origin")
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"is {other}, this push's tip is {first}; rewrite" in result.stderr, result.stderr
    assert _refs(origin) == {MAIN: other, SHADOW: other}


@pytest.mark.parametrize("mode", MODES)
def test_l1c_side_parent_merge_is_refused(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    other = _commit(root, first)
    _land(root, origin, other, MAIN, SHADOW)
    git(root, "fetch", "-q", "origin")
    merge = _commit(root, first, other, message="took origin as parent 2")
    git(root, "update-ref", "refs/heads/main", merge)
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert "not on the first-parent line of this push's tip" in result.stderr, result.stderr
    assert _refs(origin) == {MAIN: other, SHADOW: other}


@pytest.mark.parametrize("mode", MODES)
def test_l3a_stale_tracking_names_the_shadow(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    other = _commit(root, first)
    _land(root, origin, other, SHADOW)
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert (f"stale tracking: origin's {SHADOW} is {other}, this clone last recorded {first}; "
            f"run `git -C {root} fetch origin`") in result.stderr, result.stderr
    assert _refs(origin) == {MAIN: first, SHADOW: other}


@pytest.mark.parametrize("mode", MODES)
def test_l3b_offline_refuses_unverified(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    origin.rename(tmp_path / "gone.git")
    result = gr2(root, "push", "--recurse=none", check=False)
    assert result.returncode == 5, (result.returncode, result.stderr)
    assert "cannot verify origin's shadow, so nothing was pushed" in result.stderr, result.stderr
    assert git(root, "rev-parse", TRACKED).stdout.strip() == first


@pytest.mark.parametrize("mode", MODES)
def test_l3c_a_writer_after_the_check_is_turned_away(tmp_path, mode, monkeypatch):
    root, origin, first = _pushed(tmp_path, mode)
    between = _commit(root, first, message="between")
    tip = _commit(root, between, message="tip")
    git(root, "update-ref", "refs/heads/main", tip)
    git(root, "push", "-q", "--no-verify", "origin", f"{between}:refs/tmp/other")
    git(origin, "update-ref", "-d", "refs/tmp/other")
    shim, race = tmp_path / "shim", tmp_path / "race.armed"
    shim.mkdir()
    (shim / "git").write_text(
        f'#!/bin/sh\ncase " $* " in *" push "*) if [ -f "{race}" ]; then rm -f "{race}"; '
        f'"{shutil.which("git")}" -C "{origin}" update-ref {SHADOW} {between}; fi;; esac\n'
        f'exec "{shutil.which("git")}" "$@"\n')
    (shim / "git").chmod(0o755)
    race.write_text("")
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    result = gr2(root, "push", check=False)
    assert result.returncode != 0, result.stdout
    assert "stale info" in result.stderr, result.stderr
    assert not race.exists(), "the race ran"
    assert _refs(origin) == {MAIN: first, SHADOW: between}, "nothing this push sent moved"


@pytest.mark.parametrize("mode", MODES)
def test_fetch_first_names_the_command_and_then_pushes(tmp_path, mode):
    root, origin, first = _pushed(tmp_path, mode)
    git(root, "update-ref", "-d", TRACKED)
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"fetch first: origin holds {SHADOW} at {first}" in result.stderr, result.stderr
    assert f"run `git -C {root} fetch origin`, then push again" in result.stderr, result.stderr
    git(root, "fetch", "origin")
    gr2(root, "push")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(origin) == {MAIN: head, SHADOW: head}


@pytest.mark.parametrize("mode", MODES)
def test_fetch_line_is_added_once_and_said(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    result = gr2(root, "push")
    assert result.stderr.count(f"gr2: added to remote.origin.fetch: {LINE}") == 1, result.stderr
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    again = gr2(root, "push")
    assert "added to remote.origin.fetch" not in again.stderr, again.stderr
    assert _fetch_lines(root) == ["+refs/heads/*:refs/remotes/origin/*", LINE]


@pytest.mark.parametrize("mode", MODES)
def test_fetch_line_under_json_rides_the_payload_not_stderr(tmp_path, mode):
    # Regression: the stderr note broke --json for a reader of stdout and stderr together.
    root, origin = _store(tmp_path, mode)
    result = gr2(root, "push", "--json")
    assert "added to remote.origin.fetch" not in result.stderr, result.stderr
    payload = json.loads(result.stdout + result.stderr)
    assert payload["notes"] == [f"added to remote.origin.fetch: {LINE}"], payload
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    assert json.loads(gr2(root, "push", "--json").stdout)["notes"] == []


@pytest.mark.parametrize("mode", MODES)
def test_fetch_line_note_reaches_stderr_when_a_json_push_is_refused(tmp_path, mode):
    # A refused push prints no payload, so the line it added before the refusal is named on stderr beside the refusal.
    root, origin = _store(tmp_path, mode)
    _refuse_shadow(origin)
    result = gr2(root, "push", "--json", check=False)
    assert result.returncode != 0 and result.stdout == "", (result.returncode, result.stdout)
    assert LINE in _fetch_lines(root), _fetch_lines(root)
    assert f"gr2: added to remote.origin.fetch: {LINE}" in result.stderr, result.stderr


@pytest.mark.parametrize("mode", MODES)
def test_origin_that_already_has_the_line_gets_no_second_copy(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    git(root, "config", "--add", "remote.origin.fetch", LINE)
    result = gr2(root, "push")
    assert "added to remote.origin.fetch" not in result.stderr, result.stderr
    assert _fetch_lines(root).count(LINE) == 1, _fetch_lines(root)
    assert _fetch_lines(root)[0] == "+refs/heads/*:refs/remotes/origin/*", "existing refspec untouched"
