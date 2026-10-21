"""wait_for_tip polls ls-remote for the exact refs/heads/<branch> row and reports (old, new) when it moves.

A local bare repo is the remote. The clock and sleep are fakes: each sleep advances the clock and may push to the
remote, so no row waits for real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import remote_wait
from gr2.python_cli.remote_wait import CODE_REFUSED, CODE_REMOTE, CODE_TIMEOUT, RemoteWaitError, wait_for_tip


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "commit", "-q", "--allow-empty", "-m", "one")
    _git(work, "push", "-q", str(remote), "main")
    return work, remote


def _commit(work: Path, msg: str) -> str:
    _git(work, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(work, "rev-parse", "HEAD")


class Clock:
    """A fake clock; each sleep advances it and then runs the next queued action (a push, a delete)."""

    def __init__(self, *actions):
        self.now, self.actions, self.slept = 0.0, list(actions), []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.actions:
            self.actions.pop(0)()


def _wait(work, remote, branch="main", clock=None, **kw):
    clock = clock or Clock()
    return wait_for_tip(work, str(remote), branch, interval=kw.pop("interval", 1), timeout=kw.pop("timeout", 10),
                        clock=clock, sleep=clock.sleep, **kw)


def test_a_push_after_the_start_returns_the_start_tip_and_the_new_tip(world):
    work, remote = world
    start = _git(work, "rev-parse", "HEAD")
    new = []
    clock = Clock(lambda: None, lambda: (new.append(_commit(work, "two")), _git(work, "push", "-q", str(remote), "main")))
    assert _wait(work, remote, clock=clock) == (start, new[0])
    assert clock.slept == [1, 1]


def test_a_tip_already_off_from_returns_at_once_without_sleeping(world):
    work, remote = world
    old = _git(work, "rev-parse", "HEAD")
    new = _commit(work, "two")
    _git(work, "push", "-q", str(remote), "main")
    clock = Clock()
    assert _wait(work, remote, clock=clock, from_sha=old.upper()) == (old, new)
    assert clock.slept == []


def test_an_unmoved_tip_times_out_with_a_nonzero_code(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, timeout=3)
    assert exc.value.code == CODE_TIMEOUT and "still at" in str(exc.value)


def test_a_remote_starting_with_dash_is_refused_before_git_runs_it(world, tmp_path):
    work, _remote = world
    mark = tmp_path / "ran"
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, f"--upload-pack=touch {mark}")
    assert exc.value.code == CODE_REFUSED and "remote_option_shaped" in str(exc.value)
    assert not mark.exists()


def test_a_branch_starting_with_dash_is_refused(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, branch="-main")
    assert exc.value.code == CODE_REFUSED and "branch_option_shaped" in str(exc.value)


@pytest.mark.parametrize("branch", ["a..b", "a b", "a~1", "a.lock", "a:b", ""])
def test_an_invalid_refname_is_refused(world, branch):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, branch=branch)
    assert exc.value.code == CODE_REFUSED


def test_a_branch_only_present_as_a_tail_match_is_missing_not_its_lookalike(world):
    work, remote = world
    _git(work, "push", "-q", str(remote), "main:refs/heads/x/refs/heads/trunk")
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, branch="trunk")
    assert exc.value.code == CODE_REMOTE and "has no refs/heads/trunk" in str(exc.value)


def test_a_tail_match_that_moves_while_the_exact_ref_stays_is_not_moved(world):
    work, remote = world
    _git(work, "push", "-q", str(remote), "main:refs/heads/x/refs/heads/main")
    clock = Clock(lambda: (_commit(work, "two"), _git(work, "push", "-q", str(remote), "HEAD:refs/heads/x/refs/heads/main")))
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, clock=clock, timeout=3)
    assert exc.value.code == CODE_TIMEOUT


def test_a_ref_missing_at_start_is_refused(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, branch="absent")
    assert exc.value.code == CODE_REMOTE and "has no refs/heads/absent" in str(exc.value)


def test_a_ref_deleted_while_waiting_is_refused_not_moved(world):
    work, remote = world
    clock = Clock(lambda: _git(work, "push", "-q", str(remote), ":refs/heads/main"))
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, clock=clock)
    assert exc.value.code == CODE_REMOTE and "deleted while waiting" in str(exc.value)


@pytest.mark.parametrize("from_sha", ["nope", "abc1234", "g" * 40, "a" * 39, "a" * 41, "a" * 64, ""])
def test_a_from_that_is_not_a_full_40_hex_sha_is_refused(world, from_sha):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, from_sha=from_sha)
    assert exc.value.code == CODE_REFUSED and "full 40-hex" in str(exc.value)


@pytest.mark.parametrize("kind", ["interval", "timeout"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf"), "x"])
def test_a_bound_that_is_not_positive_and_finite_is_refused(world, kind, value):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, **{kind: value})
    assert exc.value.code == CODE_REFUSED and kind in str(exc.value)


@pytest.mark.parametrize("listing", ["not a row\n", "nothex\trefs/heads/main\n", "a" * 39 + "\trefs/heads/main\n"])
def test_a_malformed_ls_remote_line_is_a_fault_not_an_absent_ref(world, monkeypatch, listing):
    work, remote = world
    monkeypatch.setattr(remote_wait, "_ls_remote", lambda *_a: listing)
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote)
    assert exc.value.code == CODE_REMOTE and "not" in str(exc.value)


def test_a_u2028_lookalike_ref_does_not_spoof_the_tip(world, monkeypatch):
    work, remote = world
    real = "a" * 40
    listing = f"{real}\trefs/heads/main\n{'b' * 40}\trefs/heads/main\u2028x\n"
    monkeypatch.setattr(remote_wait, "_ls_remote", lambda *_a: listing)
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, timeout=2)
    assert exc.value.code == CODE_TIMEOUT and real in str(exc.value)


def test_credentials_in_the_remote_url_are_never_printed(world, monkeypatch):
    work, _remote = world
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    url = "https://user:s3cr3tt0ken@127.0.0.1:1/r.git"
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, url)
    assert exc.value.code == CODE_REMOTE and "s3cr3tt0ken" not in str(exc.value)


def test_credentials_in_a_remote_url_are_not_printed_on_timeout(world, monkeypatch):
    work, _remote = world
    monkeypatch.setattr(remote_wait, "_ls_remote", lambda *_a: f"{'a' * 40}\trefs/heads/main\n")
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, "https://host.example/r.git?token=s3cr3tt0ken", timeout=2)
    assert exc.value.code == CODE_TIMEOUT and "s3cr3tt0ken" not in str(exc.value)
