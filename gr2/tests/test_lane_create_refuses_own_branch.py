"""`lane create` refuses a branch that IS the repo's own branch.

A lane is the isolation primitive, and `--branch main` on a repo already on `main`
produces a lane with no isolation at all: the work commits there and `gr2 push`
writes straight to the integration branch, with the first refusal arriving at
`pr create` — after the remote has been written (measured on the
released 2.0.0a2: create 0, enter/add/commit 0, push 0 "Pushed main …", then
pr create exit 1 "head branch main is the same as base branch main").

The guard compares against what is checked out and what the clone records as its
integration branch, and it may not invent a conflict it cannot see: a repo with no
local checkout, or one whose `origin/HEAD` was never set, still creates.
"""
from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _workspace(
    tmp_path: Path,
    *,
    origin_head: bool = True,
    url: str | None = None,
    default_branch: str = "main",
) -> Path:
    """One repo, checked out on `main`, whose origin is a local bare repo.

    `origin_head=False` leaves the clone with no recorded `origin/HEAD` — the state
    of any repo cloned before it had refs, and the state the origin fallback exists
    for. `url` overrides the spec's url (an unreachable one is the cannot-tell case).
    `default_branch` changes the ORIGIN's default, which is what the ls-remote
    fallback has to parse; a slashed name is the case a last-segment split loses.
    """
    origin = tmp_path / "repo-a.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", default_branch, str(origin))
    ws = tmp_path / "ws"
    src = ws / "repos" / "repo-a"
    src.parent.mkdir(parents=True)
    # Cloned BEFORE the first push, so the remote has no refs and the clone records
    # no `origin/HEAD` — the state the fallback exists for.
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base")
    _git(src, "push", "-q", "origin", f"HEAD:refs/heads/{default_branch}")
    if origin_head:
        _git(src, "remote", "set-head", "origin", "-a")
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m"\n\n'
        '[[repos]]\nname = "repo-a"\npath = "repos/repo-a"\n'
        f'url = "{url if url is not None else origin}"\n\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["repo-a"]\n'
    )
    return ws


def test_lane_branch_equal_to_checked_out_branch_refuses(tmp_path: Path) -> None:
    """The witness: --branch main names the repo and the branch."""
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", "main"],
    )
    assert result.exit_code != 0, result.output
    assert "main" in result.output
    assert "repo-a" in result.output


def test_lane_branch_equal_to_integration_branch_refuses(tmp_path: Path) -> None:
    """Same refusal when the branch is the recorded integration branch but not the
    one currently checked out — the lane would still land there."""
    ws = _workspace(tmp_path)
    checkout = ws / "repos" / "repo-a"
    _git(checkout, "checkout", "-q", "-b", "scratch")
    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", "main"],
    )
    assert result.exit_code != 0, result.output
    assert "main" in result.output


def test_a_real_lane_branch_still_creates(tmp_path: Path) -> None:
    """Control: the guard does not refuse an ordinary lane branch."""
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", "feat/auth"],
    )
    assert result.exit_code == 0, result.output
    assert "repos/repo-a" in result.output


def test_no_recorded_origin_head_falls_back_to_the_remote(tmp_path: Path) -> None:
    """A clone with no recorded `origin/HEAD` still refuses, via the origin.

    That symref is absent in a clone made before the remote had refs — the normal
    state of a repo cloned empty and pushed later — and it is exactly the state in
    which a lane on the default branch looks harmless. The guard asks the origin
    itself, the same remote the lane is about to clone from.
    """
    ws = _workspace(tmp_path, origin_head=False)
    checkout = ws / "repos" / "repo-a"
    _git(checkout, "checkout", "-q", "-b", "scratch")
    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", "main"],
    )
    assert result.exit_code != 0, result.output
    assert "main" in result.output


def test_unreachable_origin_is_not_a_conflict(tmp_path: Path) -> None:
    """Control: when neither the checkout nor the origin can tell us, the lane creates.

    "Cannot tell" must never become a refusal: this guard refuses what it can see.
    """
    ws = _workspace(tmp_path, origin_head=False, url=str(tmp_path / "gone.git"))
    checkout = ws / "repos" / "repo-a"
    _git(checkout, "checkout", "-q", "-b", "scratch")
    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", "main"],
    )
    assert result.exit_code == 0, result.output


def _invoke_create(ws: Path, branch: str):
    return runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feat-auth", "--repos", "repo-a", "--branch", branch],
    )


def test_slashed_default_branch_from_the_origin_refuses(tmp_path: Path) -> None:
    """A DEFAULT BRANCH WHOSE NAME CONTAINS A SLASH IS STILL THE DEFAULT.

    The origin fallback parses `ref: refs/heads/release/1.x HEAD` out of
    `ls-remote --symref`. Reading that ref to its LAST SEGMENT yields `1.x`, and the
    guard then compares the requested `release/1.x` against a string the origin never
    reported, so it does not fire and the no-isolation lane is created in silence.

    The pair below differs only in the origin's default name, and the flat one is the
    control: if the flat arm ever stops refusing, this witness proves nothing.
    """
    slashed_root = tmp_path / "slashed"
    slashed_root.mkdir()
    slashed = _workspace(slashed_root, origin_head=False, default_branch="release/1.x")
    _git(slashed / "repos" / "repo-a", "checkout", "-q", "-b", "scratch")
    result = _invoke_create(slashed, "release/1.x")
    assert result.exit_code != 0, result.output
    assert "release/1.x" in result.output
    assert "repo-a" in result.output

    flat_root = tmp_path / "flat"
    flat_root.mkdir()
    flat = _workspace(flat_root, origin_head=False, default_branch="main")
    _git(flat / "repos" / "repo-a", "checkout", "-q", "-b", "scratch")
    control = _invoke_create(flat, "main")
    assert control.exit_code != 0, control.output


def test_a_silent_origin_does_not_hang_lane_create(tmp_path: Path) -> None:
    """AN ORIGIN THAT ACCEPTS AND NEVER ANSWERS MUST BOUND, NOT BLOCK.

    The doctrine is "a remote that cannot be reached gives None", but None is only
    reachable when git RETURNS. The existing cannot-tell control points at a path
    that fails instantly, so it proves the SKIP LOGIC and never touches the offline
    BEHAVIOUR: against a peer that accepts and says nothing, an unbounded ls-remote
    blocks `lane create` with no output at all.

    The fixture is a socket that accepts and never replies — deterministic and
    offline, unlike an unroutable address whose behaviour depends on the host's
    routing. Before the bound this test does not fail, it HANGS.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    # Accept into the backlog and never read or reply, on a thread that outlives the
    # command. Accepting is what makes git wait rather than fail fast.
    stop = threading.Event()

    def _accept_and_ignore() -> None:
        while not stop.is_set():
            listener.settimeout(0.2)
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            conn.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn.recv(4096)
                except (TimeoutError, OSError):
                    pass

    thread = threading.Thread(target=_accept_and_ignore, daemon=True)
    thread.start()
    try:
        ws = _workspace(
            tmp_path, origin_head=False, url=f"http://127.0.0.1:{port}/nope.git"
        )
        _git(ws / "repos" / "repo-a", "checkout", "-q", "-b", "scratch")
        started = time.monotonic()
        result = _invoke_create(ws, "main")
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        listener.close()
    assert result.exit_code == 0, result.output
    assert elapsed < 30, f"lane create took {elapsed:.1f}s against a silent origin"
