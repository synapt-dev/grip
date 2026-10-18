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


def _run(monkeypatch, ws: Path, until: str, on_sleep, timeout: str = "5") -> tuple[object, list[int]]:
    """Run the verb; on_sleep(k) runs at the k-th sleep. Returns the result and the ls-remote count of each poll."""
    polls = [0]
    real = gitops.git

    def counting(cwd, *args, **kw):
        if "ls-remote" in args:
            polls[-1] += 1
        return real(cwd, *args, **kw)

    def sleep(_s: float) -> None:
        on_sleep(len(polls))
        polls.append(0)

    monkeypatch.setattr(gitops, "git", counting)
    # sync_wait's own time, not the global module: patching time.sleep itself lets any other sleeper in the
    # process fire the hook before the start read, which made the move part of the baseline (a 1-in-~20 flake).
    monkeypatch.setattr(sync_wait, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
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
    result, polls = _run(monkeypatch, ws, "all", lambda k: _move(tmp_path, seeds, "beta") if k == 1 else None,
                         timeout="0.3")
    assert result.exit_code == sync_wait.EXIT_TIMEOUT == 6, result.output
    assert "moved: beta" in result.output
    assert "not moved: alpha, gamma" in result.output
    assert len(polls) > 2
    assert all(c <= len(NAMES) for c in polls), polls


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


@pytest.mark.parametrize("interval,timeout", [("0", "5"), ("-5", "5"), ("nan", "5"), ("1", "-1"), ("1", "nan")])
def test_an_out_of_range_interval_or_timeout_is_refused_before_any_read(tmp_path, monkeypatch, interval, timeout):
    """--interval below 1 would poll without pause and nan would never end; each is refused before any ls-remote."""
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
        if k in (1, 3):
            b.rename(g)  # polls 2 and 4 fail
        if k in (2, 4):
            g.rename(b)  # polls 3 and 5 read

    result, polls = _run(monkeypatch, ws, "all", on_sleep, timeout="0.6")
    assert result.exit_code == 6, result.output
    assert "unreadable" not in result.output
    assert len(polls) > 5, polls
