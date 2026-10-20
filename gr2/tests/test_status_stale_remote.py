"""Plain `gr2 status` must not call a member aligned while its remote holds
a commit the workspace has not fetched; it says the comparison is against
the last fetch instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _remote_with_one_commit(root: Path, name: str) -> tuple[Path, Path]:
    bare = root / f"{name}.git"
    _git(root, "init", "-q", "--bare", "-b", "trunk", str(bare))
    seed = root / f"seed-{name}"
    _git(root, "clone", "-q", str(bare), str(seed))
    (seed / "f").write_text("one\n")
    _git(seed, "add", "f")
    _git(seed, "commit", "-qm", "one")
    _git(seed, "push", "-q", "origin", "HEAD:trunk")
    return bare, seed


def test_status_does_not_say_aligned_before_fetching_a_moved_remote(tmp_path: Path, monkeypatch) -> None:
    alpha, _ = _remote_with_one_commit(tmp_path, "alpha")
    beta, beta_seed = _remote_with_one_commit(tmp_path, "beta")
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "clone", "-q", str(alpha), "alpha")
    _git(ws, "clone", "-q", str(beta), "beta")
    init = runner.invoke(app, ["workspace", "init", str(ws)])
    assert init.exit_code == 0, init.output

    # The remote moves on; the workspace has not fetched.
    (beta_seed / "f").write_text("two\n")
    _git(beta_seed, "commit", "-qam", "two")
    _git(beta_seed, "push", "-q", "origin", "HEAD:trunk")

    monkeypatch.chdir(ws)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    beta_row = next(
        line for line in result.output.splitlines() if line.split("\t")[:3] == ["shared", "beta", "beta"]
    )
    assert "aligned" not in beta_row, beta_row
    assert "as last fetched" in beta_row, beta_row
    assert "does not fetch" in beta_row, beta_row
