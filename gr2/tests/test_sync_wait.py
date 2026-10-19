"""`gr2 sync wait --all-members`: three members on bare remotes, moved between polls by the sleep hook."""
from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from gr2.python_cli import gitops, sync_wait
from gr2.python_cli.app import app

NAMES = ["alpha", "beta", "gamma"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    seeds: dict[str, Path] = {}
    spec = []
    for n in NAMES:
        bare = tmp_path / "remotes" / f"{n}.git"
        bare.parent.mkdir(exist_ok=True)
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = tmp_path / "seeds" / n
        seed.mkdir(parents=True)
        _git(seed, "init", "-q", "-b", "main")
        _git(seed, "commit", "-q", "--allow-empty", "-m", "c0")
        _git(seed, "push", "-q", str(bare), "main")
        seeds[n] = seed
        spec.append(f'[[repos]]\nname = "{n}"\npath = "{n}"\nurl = "{bare.as_uri()}"\nref = "main"\n')
    (ws / ".grip" / "workspace_spec.toml").write_text("\n".join(spec))
    return ws, seeds


def _move(tmp_path: Path, seeds: dict[str, Path], name: str) -> None:
    _git(seeds[name], "commit", "-q", "--allow-empty", "-m", "moved")
    _git(seeds[name], "push", "-q", str(tmp_path / "remotes" / f"{name}.git"), "main")


def _run(monkeypatch, ws: Path, until: str, on_sleep, timeout: str = "5", stub=None) -> tuple[object, list[int]]:
    """Run the verb; on_sleep(k) runs at the k-th sleep. Returns the result and the ls-remote count of each poll.
    stub(now, real, cwd, *args, **kw), if given, answers each git call and may advance the fake clock now[0]."""
    polls = [0]
    real = gitops.git
    now = [0.0]

    def counting(cwd, *args, **kw):
        if "ls-remote" in args:
            polls[-1] += 1
        if stub:
            return stub(now, real, cwd, *args, **kw)
        return real(cwd, *args, **kw)

    def sleep(s: float) -> None:
        on_sleep(len(polls))
        polls.append(0)
        now[0] += s

    monkeypatch.setattr(gitops, "git", counting)
    # sync_wait's own time, not the global module: patching time.sleep itself lets any other sleeper in the
    # process fire the hook before the start read, which made the move part of the baseline (a 1-in-~20 flake).
    # The clock is fake too and moves only by the sleeps, so the polls before --timeout are an exact count on any
    # host; the wall clock gave a loaded host fewer polls than a row expected.
    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", until,
                                      "--timeout", timeout, "--interval", "1"])
    return result, polls


def test_any_returns_at_the_first_poll_after_one_member_moves_and_names_it(tmp_path, monkeypatch):
    ws, seeds = _workspace(tmp_path)
    result, polls = _run(monkeypatch, ws, "any", lambda k: _move(tmp_path, seeds, "beta") if k == 1 else None)
    assert result.exit_code == 0, result.output
    assert "moved: beta" in result.output and "met: any: moved beta" in result.output
    assert "alpha" not in result.output.split("met:")[1]
    assert len(polls) == 2  # the poll after the move returns
    assert all(c <= len(NAMES) for c in polls), polls


def test_all_keeps_waiting_while_one_has_moved_and_times_out_with_its_own_exit(tmp_path, monkeypatch):
    ws, seeds = _workspace(tmp_path)
    beta_reads = []

    def counting_beta(now, real, cwd, *args, **kw):
        if "ls-remote" in args and "/beta.git" in args[-2]:
            beta_reads.append(now[0])
        return real(cwd, *args, **kw)

    result, polls = _run(monkeypatch, ws, "all", lambda k: _move(tmp_path, seeds, "beta") if k == 1 else None,
                         timeout="3", stub=counting_beta)
    assert beta_reads == [0.0, 1.0], beta_reads  # the start read, then the poll that sees it move; never again
    assert result.exit_code == sync_wait.EXIT_TIMEOUT == 6, result.output
    assert "moved: beta" in result.output
    assert "not moved as of the last read: alpha at 2.9s, gamma at 2.9s" in result.output
    # start reads, then polls at 1 s (beta seen to move) and 2 s, the last with a full read (1 s) before the
    # deadline; then the wait sleeps out --timeout without a read: beta is not read again
    assert polls == [3, 3, 2, 2, 2], polls


def test_a_slow_remote_that_never_moves_times_out_as_not_moved_not_as_a_failed_read(tmp_path, monkeypatch):
    """Remotes that answer in 0.5 s and never move: no poll lands on the deadline with a 0.1 s read, so none fails."""
    ws, _seeds = _workspace(tmp_path)
    real = gitops.git

    def answers_in_half_a_second(now, real, cwd, *args, **kw):
        if "ls-remote" in args:
            bound = kw.get("timeout", 600)
            now[0] += min(bound, 0.5)  # a bounded read spends its bound (or its 0.5 s answer) on the fake clock
            if bound < 0.5:
                return subprocess.CompletedProcess(args, 128, "", f"fatal: git timed out after {bound}s")
        return real(cwd, *args, **kw)

    result, polls = _run(monkeypatch, ws, "all", lambda k: None, timeout="3", stub=answers_in_half_a_second)
    assert result.exit_code == 6, result.output
    assert "last read failed" not in result.output, result.output
    assert "all not met; not moved as of the last read: alpha at 2s, beta at 2.5s, gamma at 3s" in result.output, result.output
    assert polls == [3, 3], polls  # 1.5 s of start reads, one interval sleep, 1.5 s of reads to the deadline


def test_all_returns_once_every_member_has_moved(tmp_path, monkeypatch):
    ws, seeds = _workspace(tmp_path)

    def on_sleep(k: int) -> None:
        if k == 1:
            _move(tmp_path, seeds, "gamma")
        if k == 2:
            _move(tmp_path, seeds, "alpha")
            _move(tmp_path, seeds, "beta")

    result, polls = _run(monkeypatch, ws, "all", on_sleep)
    assert result.exit_code == 0, result.output
    assert "met: all: moved gamma, alpha, beta" in result.output
    assert polls[1] == 3 and polls[2] == 2  # a member seen to move is not polled again


def test_a_refusal_exits_3_from_the_sync_exit_table_and_a_usage_error_stays_2(tmp_path):
    """3 is the sync group's `refused`; 2 stays click's own usage error, so a caller can tell the two apart."""
    from gr2.python_cli.exit_codes import EXIT_CODES

    assert EXIT_CODES["sync"][sync_wait.EXIT_REFUSED] == "refused" and EXIT_CODES["sync"][2] == "usage"
    ws, _ = _workspace(tmp_path)
    runner = CliRunner()
    no_flag = runner.invoke(app, ["sync", "wait", str(ws)])
    assert no_flag.exit_code == 3 and no_flag.stderr.startswith("refused: "), no_flag.output
    spec = ws / ".grip" / "workspace_spec.toml"
    spec.write_text('[[repos]]\nname = "alpha"\npath = "alpha"\nref = "main"\n')
    no_url = runner.invoke(app, ["sync", "wait", str(ws), "--all-members", "--timeout", "1"])
    assert no_url.exit_code == 3 and "refused: no url for: alpha" in no_url.output, no_url.output
    usage = runner.invoke(app, ["sync", "wait", str(ws), "--timeout", "soon"])
    assert usage.exit_code == 2, usage.output


def test_a_remote_that_vanishes_mid_wait_ends_it_unreadable_not_as_a_timeout(tmp_path, monkeypatch):
    ws, seeds = _workspace(tmp_path)
    gone = tmp_path / "remotes" / "beta.git"
    result, polls = _run(monkeypatch, ws, "all", lambda k: gone.rename(gone.with_name("gone.git")) if k == 1 else None)
    assert result.exit_code == sync_wait.EXIT_UNREADABLE == 7, result.output
    line = next(ln for ln in result.output.splitlines() if ln.startswith("unreadable: "))
    assert line.startswith("unreadable: beta ") and len(line) > len("unreadable: beta "), line
    assert "timeout" not in result.output and "not moved" not in result.output
    assert len(polls) == 3  # read at poll 1, failed at poll 2 (retried), failed at poll 3 (ends)


def test_a_checkout_behind_its_remote_at_start_reports_moved_at_the_first_poll(tmp_path, monkeypatch):
    ws, seeds = _workspace(tmp_path)
    _git(tmp_path, "clone", "-q", str(tmp_path / "remotes" / "alpha.git"), str(ws / "alpha"))
    seen = _git(ws / "alpha", "rev-parse", "refs/remotes/origin/main")
    _move(tmp_path, seeds, "alpha")
    now = _git(seeds["alpha"], "rev-parse", "HEAD")
    result, polls = _run(monkeypatch, ws, "any", lambda k: None)
    assert result.exit_code == 0, result.output
    assert f"moved: alpha {seen[:12]} -> {now[:12]}" in result.output
    assert len(polls) == 1


def _spec(ws: Path, url: str) -> None:
    (ws / ".grip" / "workspace_spec.toml").write_text(f'[[repos]]\nname = "bad"\npath = "bad"\nurl = "{url}"\nref = "main"\n')


def test_a_member_url_that_is_a_git_option_is_read_as_a_repository_never_run(tmp_path):
    """A url `--upload-pack=touch <marker>` is passed after `--`, so git never runs the command."""
    ws, _seeds = _workspace(tmp_path)
    marker = tmp_path / "marker"
    _spec(ws, f"--upload-pack=touch {marker}")
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--timeout", "5"])
    assert not marker.exists(), "the member url ran as a git option"
    assert result.exit_code == sync_wait.EXIT_REFUSED == 3, result.output
    assert "refused: cannot read refs/heads/main of bad" in result.stderr and ": " in result.stderr.split(" at ", 1)[1]
    assert result.stdout == ""


def test_a_remote_that_accepts_and_never_answers_ends_by_the_timeout(tmp_path):
    """A remote that never answers ends the start read by --timeout, not by gitops' 600 s default."""
    ws, _seeds = _workspace(tmp_path)
    listener = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    _spec(ws, f"git://127.0.0.1:{listener.getsockname()[1]}/x")
    start = time.monotonic()
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--timeout", "1"])
    took = time.monotonic() - start
    listener.close()
    assert took < 10, took
    assert result.exit_code == sync_wait.EXIT_TIMEOUT == 6, result.output
    assert "timeout after 1s reading bad at the start: git timed out" in result.stderr


@pytest.mark.parametrize("interval,timeout", [("0", "5"), ("-5", "5"), ("nan", "5"), ("0.5", "5"), ("1", "-1"), ("1", "nan"), ("1", "inf")])
def test_an_out_of_range_interval_or_timeout_is_refused_before_any_read(tmp_path, monkeypatch, interval, timeout):
    """--interval below 1 would poll without pause and nan or inf would never end; each is refused before any ls-remote."""
    ws, _seeds = _workspace(tmp_path)
    reads = [0]
    real = gitops.git

    def counting(cwd, *args, **kw):
        reads[0] += "ls-remote" in args
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "git", counting)
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", "all",
                                      "--timeout", timeout, "--interval", interval])
    assert result.exit_code == sync_wait.EXIT_REFUSED == 3, result.output
    assert result.stderr.startswith("refused: --") and result.stdout == ""
    assert reads[0] == 0


def test_a_flaky_remote_that_fails_twice_but_never_in_a_row_runs_to_the_timeout(tmp_path, monkeypatch):
    """Fail, read, fail is not unreadable: only two failures in a row end the wait."""
    ws, _seeds = _workspace(tmp_path)
    b = tmp_path / "remotes" / "beta.git"
    g = b.with_name("gone.git")

    def on_sleep(k: int) -> None:
        if k in (1, 3, 5):
            b.rename(g)  # polls 2, 4 and 6 fail
        if k in (2, 4, 6):
            g.rename(b)  # polls 3, 5 and 7 read

    result, polls = _run(monkeypatch, ws, "all", on_sleep, timeout="5")
    assert result.exit_code == 6, result.output
    assert "unreadable" not in result.output
    assert "not moved as of the last read: alpha at 4.9s, beta at 4.9s, gamma at 4.9s" in result.output
    # start reads, then 1 s to 4 s and the two near-deadline polls (4.x s, 4.9 s): fail, read, fail, read, fail, read
    assert polls == [3, 3, 3, 3, 3, 3, 3], polls


def _hang_clock(monkeypatch, hung: str):
    """A fake clock for the rows whose poll reads hang on a real listener: it moves by the sleeps and, for a read
    sent to the listener, by that read's bound (git's own timeout ran it in real time). The start reads cost 0 s,
    so a member last read at the start reads as at 0s however long setup took on a loaded host."""
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))

    def ran(proc, args, kw):
        if hung in args:
            now[0] += kw["timeout"]
        return proc
    return ran


def test_a_remote_that_stops_answering_mid_wait_ends_by_the_timeout_and_is_not_called_not_moved(tmp_path, monkeypatch):
    """Start reads answer, every poll read hangs: --timeout bounds the poll reads, and a failed last read is not "not moved"."""
    ws, _seeds = _workspace(tmp_path)
    listener = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    hung = f"git://127.0.0.1:{listener.getsockname()[1]}/x"
    real, reads = gitops.git, [0]

    def hangs_after_the_start(cwd, *args, **kw):
        if "ls-remote" in args:
            reads[0] += 1
            if reads[0] > 3:  # the three start reads answer; every poll read goes to the listener
                args = tuple(hung if a.startswith("file:") or a.startswith("/") else a for a in args)
        return clocked(real(cwd, *args, **kw), args, kw)

    monkeypatch.setattr(gitops, "git", hangs_after_the_start)
    clocked = _hang_clock(monkeypatch, hung)
    start = time.monotonic()
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", "all", "--timeout", "2",
                                      "--interval", "1"])  # poll 1 at 1 s leaves a 1 s read before the deadline
    took = time.monotonic() - start
    listener.close()
    assert took < 10, took
    assert result.exit_code == 6, result.output
    assert "alpha" not in result.output.split("not moved as of the last read: ")[1].split(";")[0], result.output
    assert "all not met; not moved as of the last read: beta at 0s, gamma at 0s; last read failed: alpha (git timed out" in result.output, result.output

def test_mixed_timeout_names_not_moved_and_failed_apart(tmp_path, monkeypatch):
    """Alpha's poll reads hang while beta and gamma answer unmoved: the timeout line names each in its own clause."""
    ws, _seeds = _workspace(tmp_path)
    listener = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    hung = f"git://127.0.0.1:{listener.getsockname()[1]}/x"
    real, reads = gitops.git, [0]

    def alpha_hangs_after_the_start(cwd, *args, **kw):
        if "ls-remote" in args:
            reads[0] += 1
            if reads[0] > 3:
                args = tuple(hung if "alpha.git" in a else a for a in args)
        return clocked(real(cwd, *args, **kw), args, kw)

    monkeypatch.setattr(gitops, "git", alpha_hangs_after_the_start)
    clocked = _hang_clock(monkeypatch, hung)
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", "all", "--timeout", "2",
                                      "--interval", "1"])  # poll 1 at 1 s leaves a 1 s read before the deadline
    listener.close()
    assert result.exit_code == 6, result.output
    assert "all not met; not moved as of the last read: beta at 0s, gamma at 0s; last read failed: alpha (git timed out" in result.output, result.output


def _timed(monkeypatch, ws: Path, until: str, *, per_read: float, timeout: str, interval: str, moves: dict | None = None):
    """A fake clock that each ls-remote ADVANCES by per_read (or by its bound, if shorter: a timed-out read); a member
    in moves reads as moved once the clock is past its time. Sleeps advance the clock too."""
    now, real, moves = [0.0], gitops.git, moves or {}

    def read(cwd, *args, **kw):
        if "ls-remote" not in args:
            return real(cwd, *args, **kw)
        if kw["timeout"] < per_read:
            now[0] += kw["timeout"]
            return subprocess.CompletedProcess(args, 128, "", f"fatal: git timed out after {kw['timeout']:g}s")
        now[0] += per_read
        proc = real(cwd, *args, **kw)
        if any(f"/{n}.git" in args[-2] and now[0] >= t for n, t in moves.items()):
            proc = subprocess.CompletedProcess(args, 0, "f" * 40 + "\trefs/heads/main\n", "")
        return proc

    def sleep(s: float) -> None:
        now[0] += s

    monkeypatch.setattr(gitops, "git", read)
    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))
    return CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", until,
                                    "--timeout", timeout, "--interval", interval])


def test_p3_reads_that_take_clock_time_are_never_cut_into_failed_and_name_their_last_read(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _timed(monkeypatch, ws, "all", per_read=0.5, timeout="3", interval="1")
    assert result.exit_code == 6, result.output
    assert "failed" not in result.output, result.output
    assert "not moved as of the last read: alpha at 2s, beta at 2.5s, gamma at 3s" in result.output, result.output


def test_p2_a_timeout_at_the_default_interval_still_polls_before_the_deadline(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _timed(monkeypatch, ws, "any", per_read=0.5, timeout="30", interval="30", moves={"beta": 10})
    assert result.exit_code == 0, result.output
    assert "met: any: moved beta" in result.output, result.output


def test_p1_a_move_after_the_last_interval_poll_is_met_by_the_poll_sized_to_end_by_the_deadline(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _timed(monkeypatch, ws, "any", per_read=0.5, timeout="100", interval="30", moves={"gamma": 85})
    assert result.exit_code == 0, result.output
    assert "met: any: moved gamma" in result.output, result.output


def test_p4_slow_reads_at_the_deadline_are_not_failed_reads(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _timed(monkeypatch, ws, "all", per_read=12, timeout="120", interval="30")
    assert result.exit_code == 6, result.output
    assert "failed" not in result.output, result.output
    assert "not moved as of the last read: alpha at 114s, gamma at 102s; not read before the deadline: beta" in result.output, result.output


def _beta_from(monkeypatch, ws: Path, *, at: float, stall: bool, timeout: str):
    """Reads take 0.5 s on a fake clock; from the time at, beta's read fails: at once, or (stall) only when its bound,
    the time left to the deadline, runs out."""
    now, real = [0.0], gitops.git

    def read(cwd, *args, **kw):
        if "ls-remote" not in args:
            return real(cwd, *args, **kw)
        if "/beta.git" in args[-2] and now[0] >= at:
            if stall:
                now[0] += kw["timeout"]
                return subprocess.CompletedProcess(args, 128, "", f"fatal: git timed out after {kw['timeout']:g}s")
            return subprocess.CompletedProcess(args, 128, "", "fatal: repository not found")
        now[0] += 0.5
        return real(cwd, *args, **kw)

    def sleep(s: float) -> None:
        now[0] += s

    monkeypatch.setattr(gitops, "git", read)
    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))
    return CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", "all",
                                    "--timeout", timeout, "--interval", "1"])


def test_p5_a_fast_second_failure_in_the_last_poll_is_unreadable_not_cut(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _beta_from(monkeypatch, ws, at=2.0, stall=False, timeout="4")
    assert result.exit_code == 7, result.output
    assert "unreadable: beta fatal: repository not found" in result.output, result.output


def test_p7_timeout_zero_reports_the_start_reads(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    result = _timed(monkeypatch, ws, "all", per_read=0.05, timeout="0", interval="1")
    assert result.exit_code == 6, result.output
    assert "timeout after 0s: all not met; not moved as of the last read: alpha at 0.2s, beta at 0.2s, gamma at 0.2s" in result.output, result.output


def test_p8_a_cut_read_after_a_failed_read_keeps_the_failure(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    now, real, beta = [0.0], gitops.git, []

    def read(cwd, *args, **kw):
        if "ls-remote" not in args:
            return real(cwd, *args, **kw)
        if "/beta.git" in args[-2] and now[0] >= 2.0:
            beta.append(now[0])
            if len(beta) == 1:
                now[0] += 0.5
                return subprocess.CompletedProcess(args, 128, "", "fatal: repository not found")
            now[0] += kw["timeout"]
            return subprocess.CompletedProcess(args, 124, "", f"git timed out after {kw['timeout']}s")
        now[0] += 0.5
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "git", read)
    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=lambda s: now.__setitem__(0, now[0] + s), monotonic=lambda: now[0]))
    result = CliRunner().invoke(app, ["sync", "wait", str(ws), "--all-members", "--until", "all", "--timeout", "5", "--interval", "1"])
    assert len(beta) == 2, (beta, result.output)
    assert result.exit_code == 6, result.output
    assert "last read failed: beta (fatal: repository not found)" in result.output, result.output
    assert "not read before the deadline" not in result.output, result.output
