"""The README's "First five minutes" says every command in it was run, in order. This runs them.

The commands are read from README.md itself, so the README and this test cannot drift apart: a command
that refuses, or an output block that no longer matches what the command prints, turns this file red.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

README = Path(__file__).resolve().parents[1] / "README.md"
runner = CliRunner()


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## First five minutes")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _blocks(section: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"```(\w*)\n(.*?)```", section, re.S)]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def ws(tmp_path: Path, monkeypatch) -> Path:
    """Two clones side by side in one folder, as the README asks, each with a remote."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".gitconfig").write_text("[user]\n\tname = You\n\temail = you@example.com\n")
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("repo-a", "repo-b"):
        remote = tmp_path / f"{name}.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
        _git(tmp_path, "clone", "-q", str(remote), str(root / name))
        clone = root / name
        _git(clone, "config", "user.email", "you@example.com")
        _git(clone, "config", "user.name", "You")
        _git(clone, "checkout", "-q", "-B", "main")
        (clone / "README").write_text(f"{name}\n")
        _git(clone, "add", "README")
        _git(clone, "commit", "-qm", "init")
        _git(clone, "push", "-q", "origin", "main")
    return root


def _run_block(block: str, root: Path, monkeypatch) -> dict[str, str]:
    """Each `gr2 ...` line of a README bash block, with ~/ws as the fixture root; returns stdout per verb."""
    out = {}
    for line in block.splitlines():
        line = line.split("  #", 1)[0].strip()
        if not line:
            continue
        line = line.replace("~/ws", str(root))
        if line.startswith("cd "):
            monkeypatch.chdir(line[3:].strip())
            continue
        argv = shlex.split(line)
        assert argv[0] == "gr2", f"README walk line is not a gr2 command: {line!r}"
        result = runner.invoke(app, argv[1:], catch_exceptions=False)
        assert result.exit_code == 0, f"README line {line!r} exited {result.exit_code}:\n{result.output}"
        out[" ".join(argv[1:3])] = result.stdout
    return out


def test_the_readme_walk_runs_in_order_and_prints_what_it_shows(ws: Path, monkeypatch) -> None:
    blocks = _blocks(_section())
    bash = [body for lang, body in blocks if lang == "bash"]
    printed = [body for lang, body in blocks if lang == ""]
    assert len(bash) >= 2 and printed, "fixture check: the section's setup, lane and output blocks were found"

    _run_block(bash[0], ws, monkeypatch)
    lane = _run_block(bash[1], ws, monkeypatch)

    shown = [line.replace("~/ws", str(ws)) for line in printed[0].strip().splitlines()]
    assert lane["lane create"].strip().splitlines() == shown, (
        "the README's printed block must be what `lane create` prints"
    )
    for line in shown:
        path = Path(line.split(": ", 1)[-1])
        assert path.exists(), f"the README names a path that does not exist after the walk: {path}"


def test_workspace_status_names_each_declared_repo_once_after_materialize(ws: Path, monkeypatch) -> None:
    """A materialized workspace also has a clone of each repo in the default unit; the declaration is one per repo."""
    monkeypatch.chdir(ws)
    assert runner.invoke(app, ["workspace", "init", str(ws)]).exit_code == 0
    assert runner.invoke(app, ["workspace", "materialize", str(ws), "--yes"]).exit_code == 0
    assert (ws / "agents" / "default" / "home" / "repo-a").is_dir(), "fixture check: the unit clone exists"

    status = runner.invoke(app, ["workspace", "status", str(ws)])

    assert status.exit_code == 0, status.output
    declared = [line for line in status.stdout.splitlines() if line.startswith("declared = ")]
    assert [line.split()[2] for line in declared] == ["repo-a", "repo-b"], declared
