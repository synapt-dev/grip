"""Rows for `gr2 review wait` and remote_wait's sync-wait contract: 0 moved, 3 refused, 6 timeout, 7 unreadable."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from gr2.python_cli import remote_wait
from gr2.python_cli.exit_codes import EXIT_CODES
from gr2.python_cli.remote_wait import RemoteWaitError, wait_for_tip


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


class Clock:
    def __init__(self, *actions):
        self.now, self.actions, self.slept = 0.0, list(actions), []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.actions:
            self.actions.pop(0)()


def _wait(work, remote, clock=None, **kw):
    clock = clock or Clock()
    return wait_for_tip(work, str(remote), kw.pop("branch", "main"), interval=kw.pop("interval", 1),
                        timeout=kw.pop("timeout", 10), clock=clock, sleep=clock.sleep, **kw)


def _push(work, remote, msg="two"):
    def act():
        _git(work, "commit", "-q", "--allow-empty", "-m", msg)
        _git(work, "push", "-q", str(remote), "main")
    return act


def test_the_codes_are_sync_waits():
    assert (remote_wait.CODE_REFUSED, remote_wait.CODE_TIMEOUT, remote_wait.CODE_UNREADABLE) == (3, 6, 7)
    assert EXIT_CODES["review wait"] == EXIT_CODES["sync"]


def test_the_codes_are_keyed_by_the_verb_not_the_review_group():
    # The other review verbs exit 1 or 4; a "review" key would publish 0/2/3/6/7 as their contract too.
    assert "review" not in EXIT_CODES
    api = (Path(__file__).resolve().parents[1] / "api" / "cli.api").read_text()
    assert re.search(r"^exit  review wait 6 timeout\s", api, re.M)
    assert not re.search(r"^exit  review \d", api, re.M)


def test_moved_returns_old_and_new(world):
    work, remote = world
    start = _git(work, "rev-parse", "HEAD")
    old, new = _wait(work, remote, clock=Clock(_push(work, remote)))
    assert old == start and new == _git(work, "rev-parse", "HEAD") != start


def test_unmoved_times_out_6(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, timeout=3)
    assert exc.value.code == 6 and "still at" in str(exc.value)


def test_timeout_zero_is_allowed_and_times_out_after_one_read(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, timeout=0)
    assert exc.value.code == 6


def test_since_an_older_sha_returns_at_once(world):
    work, remote = world
    first = _git(work, "rev-parse", "HEAD")
    _push(work, remote)()
    clock = Clock()
    assert _wait(work, remote, clock=clock, from_sha=first.upper())[0] == first and clock.slept == []


@pytest.mark.parametrize("kind,value", [("timeout", -1), ("timeout", float("nan")), ("timeout", float("inf")),
                                        ("interval", 0.5), ("interval", 0), ("interval", float("nan")),
                                        ("interval", float("inf")), ("interval", -1)])
def test_bad_bounds_are_refused_3_before_any_read(world, monkeypatch, kind, value):
    work, remote = world
    calls = []
    monkeypatch.setattr(remote_wait.gitops, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, **{kind: value})
    assert exc.value.code == 3 and f"--{kind}" in str(exc.value) and calls == []


def _scripted(monkeypatch, answers):
    """ls-remote answers in order: a str is stdout (rc 0), an int is a failing rc with stderr 'boom'."""
    real = remote_wait.gitops.run

    def run(cwd, *args, **kw):
        if args[0] != "ls-remote":
            return real(cwd, *args, **kw)
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(a, int):
            return subprocess.CompletedProcess(args, a, stdout="", stderr="fatal: boom\n")
        return subprocess.CompletedProcess(args, 0, stdout=a, stderr="")
    monkeypatch.setattr(remote_wait.gitops, "run", run)


TIP = f"{'a' * 40}\trefs/heads/main\n"
MOVED = f"{'b' * 40}\trefs/heads/main\n"


def test_two_failed_reads_in_a_row_are_unreadable_7(world, monkeypatch):
    work, remote = world
    _scripted(monkeypatch, [TIP, 128, 128])
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote)
    assert exc.value.code == 7 and "boom" in str(exc.value)


def test_one_failed_read_is_retried_not_counted_as_not_moved(world, monkeypatch):
    work, remote = world
    _scripted(monkeypatch, [TIP, 128, MOVED])
    assert _wait(work, remote) == ("a" * 40, "b" * 40)


def test_failures_must_be_in_a_row(world, monkeypatch):
    work, remote = world
    _scripted(monkeypatch, [TIP, 128, TIP, 128, MOVED])
    assert _wait(work, remote)[1] == "b" * 40


def test_a_ref_gone_mid_poll_is_7_with_its_named_line(world):
    work, remote = world
    clock = Clock(lambda: _git(work, "push", "-q", str(remote), ":refs/heads/main"))
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, clock=clock)
    msg = str(exc.value)
    assert exc.value.code == 7 and "was deleted while waiting" in msg and "merged" in msg and "--contains" in msg


def test_a_lookalike_ref_is_not_the_branch(world):
    work, remote = world
    _git(work, "push", "-q", str(remote), "main:refs/heads/x/refs/heads/main")
    _git(work, "push", "-q", str(remote), ":refs/heads/main")
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote)
    assert exc.value.code == 3 and "has no refs/heads/main" in str(exc.value)


def test_missing_at_start_is_refused_3(world):
    work, remote = world
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, remote, branch="absent")
    assert exc.value.code == 3


def test_dashdash_keeps_an_option_shaped_url_a_repo_name(world, tmp_path, monkeypatch):
    work, _remote = world
    witness = tmp_path / "witness"
    seen = []
    real = remote_wait.gitops.run

    def run(cwd, *args, **kw):
        if args[0] == "ls-remote":
            seen.append(args)
        return real(cwd, *args, **kw)
    monkeypatch.setattr(remote_wait.gitops, "run", run)
    url = f"--upload-pack=touch {witness}"
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, url)
    assert exc.value.code == 3 and not witness.exists()
    assert all(a[1] == "--" for a in seen)


def test_credentials_are_redacted_at_the_start(world, monkeypatch):
    work, _remote = world
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    with pytest.raises(RemoteWaitError) as exc:
        _wait(work, "https://user:s3cr3tt0ken@127.0.0.1:1/r.git")
    assert exc.value.code == 3 and "s3cr3tt0ken" not in str(exc.value)


def test_each_read_is_bounded_by_min_30s_and_time_left(world, monkeypatch):
    work, remote = world
    seen = []
    real = remote_wait.gitops.run

    def run(cwd, *args, **kw):
        if args[0] == "ls-remote":
            seen.append(kw.get("timeout"))
            return subprocess.CompletedProcess(args, 0, stdout=TIP, stderr="")
        return real(cwd, *args, **kw)
    monkeypatch.setattr(remote_wait.gitops, "run", run)
    with pytest.raises(RemoteWaitError):
        _wait(work, remote, timeout=100, interval=60)
    assert seen[0] == 30 and seen[1] == 30 and seen[2] < 30


def _cli(cwd, *args, timeout=60):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), COLUMNS="400", TERM="dumb")
    return subprocess.run([sys.executable, "-m", "gr2.python_cli", "review", "wait", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def test_cli_end_to_end_moved_timeout_refused(world):
    work, remote = world
    first = _git(work, "rev-parse", "HEAD")
    _push(work, remote)()
    moved = _cli(work, str(remote), "main", "--since", first)
    assert moved.returncode == 0, moved.stderr
    assert moved.stdout.split() == ["moved:", "main", first, "->", _git(work, "rev-parse", "HEAD")]
    timed = _cli(work, str(remote), "main", "--timeout", "0")
    assert timed.returncode == 6 and "timeout" in timed.stderr
    refused = _cli(work, str(remote), "main", "--interval", "0.5")
    assert refused.returncode == 3 and "--interval" in refused.stderr
    help_text = strip_ansi(_cli(work, "--help").stdout)
    help_text = " ".join(re.sub(r"[─-╿]", " ", help_text).split()).lower()
    assert "same codes as sync wait" in help_text
