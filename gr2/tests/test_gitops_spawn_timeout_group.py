"""A timeout kills everything git started, not git alone, when git runs in its own process group."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gr2.python_cli import gitops
from gr2.python_cli.gitops import _own_group

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


def _stat(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return raw[raw.rindex(")") + 2:].split()  # state, ppid, pgrp, ...


def _group_alive(pgrp: int) -> list[int]:
    alive = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat(int(entry.name))
            if fields and int(fields[2]) == pgrp and fields[0] != "Z":
                alive.append(int(entry.name))
    return alive


def _slow_ls_remote(tmp_path: Path, **kwargs):
    """ls-remote whose upload-pack (a grandchild of gr2) records git's pid and its group, then sleeps."""
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    record = tmp_path / "ids"
    pack = f"sh -c 'cut -d\" \" -f4,5 /proc/$$/stat > {record}; sleep 30; exec git-upload-pack \"$@\"' x"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    started = time.monotonic()
    outcome = gitops.run(work, "-c", "protocol.file.allow=always", "ls-remote", f"--upload-pack={pack}", str(bare),
                         env=env, timeout=2, **kwargs)
    return outcome, time.monotonic() - started, record


def _ids(record: Path) -> tuple[int, int]:
    git_pid, pgrp = record.read_text().split()
    return int(git_pid), int(pgrp)


def _gone_within(pgrp: int, seconds: float) -> list[int]:
    deadline = time.monotonic() + seconds
    while (alive := _group_alive(pgrp)) and time.monotonic() < deadline:
        time.sleep(0.05)
    return alive


@linux
def test_timeout_kills_the_transport_group(tmp_path):
    proc, elapsed, record = _slow_ls_remote(tmp_path)
    assert proc.returncode == 124
    assert elapsed < 10
    _, pgrp = _ids(record)
    assert pgrp != os.getpgrp()
    assert _gone_within(pgrp, 1.0) == []


@linux
def test_raise_timeout_raises_and_kills_the_group(tmp_path):
    started = time.monotonic()
    with pytest.raises(Exception) as caught:
        _slow_ls_remote(tmp_path, raise_timeout=True)
    assert time.monotonic() - started < 10
    _, pgrp = _ids(tmp_path / "ids")
    assert pgrp != os.getpgrp()
    assert _gone_within(pgrp, 1.0) == []
    assert isinstance(caught.value, subprocess.TimeoutExpired)


@linux
def test_git_is_reaped_not_a_zombie(tmp_path):
    proc, _, record = _slow_ls_remote(tmp_path)
    assert proc.returncode == 124
    git_pid, _ = _ids(record)
    fields = _stat(git_pid)
    # a zombie orphaned to a PID 1 that does not reap is not ours; one whose parent is this process is
    assert fields is None or fields[0] != "Z" or int(fields[1]) != os.getpid()


def test_a_call_that_does_not_time_out_is_unchanged(tmp_path):
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ok = gitops.run(work, "rev-parse", "--is-inside-work-tree", env=env)
    assert (ok.returncode, ok.stdout.strip()) == (0, "true")
    bad = gitops.run(work, "ls-remote", "https://user:s3cretTok@127.0.0.1:1/x.git", env=env, timeout=20)
    assert bad.returncode not in (0, 124)
    assert bad.stderr
    assert "s3cretTok" not in bad.stderr


class _Tty:
    def isatty(self):
        return True


def test_own_group_when_prompts_are_off(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Tty())
    assert _own_group({"GIT_TERMINAL_PROMPT": "0"}) is True


def test_no_own_group_on_a_tty_that_may_prompt(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    assert _own_group(None) is False
    assert _own_group({}) is False


def test_own_group_when_stdin_is_none(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert _own_group({}) is True


def _alive(pid: int) -> bool:
    # A zombie whose parent died is gone for our purpose; ps works on Linux and macOS alike.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_an_interrupt_kills_the_group_too(tmp_path):
    # Ctrl-C reaches the terminal's foreground group, which git's own session has left, so _run_group kills it.
    import signal
    pidfile = tmp_path / "pid"
    argv = ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"]

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        with pytest.raises(KeyboardInterrupt):
            gitops._run_group(argv, cwd=tmp_path, input=None, env=None, text=True, timeout=10)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 2
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def _refuse_killpg(pid, sig):
    raise PermissionError(1, "Operation not permitted")


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_a_refused_group_kill_still_times_out(monkeypatch, tmp_path):
    # macOS answers killpg on a zombie-only group with EPERM; the call must still end as a timeout.
    monkeypatch.setattr(gitops.os, "killpg", _refuse_killpg)
    with pytest.raises(subprocess.TimeoutExpired):
        gitops._run_group(["sleep", "5"], cwd=tmp_path, input=None, env=None, text=True, timeout=0.2)


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_a_refused_group_kill_still_raises_the_interrupt(monkeypatch, tmp_path):
    import signal
    monkeypatch.setattr(gitops.os, "killpg", _refuse_killpg)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    try:
        with pytest.raises(KeyboardInterrupt):
            gitops._run_group(["sleep", "5"], cwd=tmp_path, input=None, env=None, text=True, timeout=10)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_no_own_group_off_posix(monkeypatch):
    # Windows has no session and no killpg, so a timed call keeps the single-process kill there.
    monkeypatch.setattr(gitops, "_POSIX", False)
    monkeypatch.setattr(sys, "stdin", None)
    assert _own_group({"GIT_TERMINAL_PROMPT": "0"}) is False


def _repo_with_a_commit(tmp_path: Path) -> Path:
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "one"], check=True)
    return work


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_the_group_path_answers_as_run_does(tmp_path):
    # conftest keeps the modules that stub subprocess.run on that path; this row holds the group path to the same
    # answer for a real git that succeeds and one that fails: returncode, stdout and stderr all equal.
    work = _repo_with_a_commit(tmp_path)
    for argv, rc in ((["git", "ls-remote", str(work)], 0), (["git", "ls-remote", str(tmp_path / "missing")], 128)):
        plain = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=10)
        group = gitops._run_group(argv, cwd=tmp_path, input=None, env=None, text=True, timeout=10)
        assert plain.returncode == rc, plain
        assert (group.returncode, group.stdout, group.stderr) == (plain.returncode, plain.stdout, plain.stderr)


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_a_timed_real_git_call_takes_the_group_path(monkeypatch, tmp_path):
    # With no terminal to prompt on (GIT_TERMINAL_PROMPT=0, as in CI), a timed call leads its own group.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    work = _repo_with_a_commit(tmp_path)
    calls = []
    real = gitops._run_group

    def spy(argv, **kwargs):
        calls.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(gitops, "_run_group", spy)
    done = gitops.run(tmp_path, "ls-remote", str(work))
    assert done.returncode == 0 and "refs/heads/main" in done.stdout, done
    assert len(calls) == 1, calls


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX-only")
def test_a_descendant_in_its_own_session_holding_stdout_does_not_hang_the_timeout(tmp_path):
    # The group kill cannot reach a child that left git's session; it still holds the inherited stdout pipe, so an
    # unbounded drain after the kill waits for it (20 s here, forever for a daemon). The timeout must hold anyway.
    pidfile = tmp_path / "stray.pid"
    fake = tmp_path / "fakegit.py"
    fake.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n", encoding="utf-8")
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            gitops._run_group([sys.executable, str(fake)], cwd=None, input=None, env=None, text=True, timeout=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 5, f"timeout 1 s took {elapsed:.1f} s"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass
