"""An AdapterError reaching the console entry point prints one sentence, not a traceback.

Adapters are the part of this CLI the team does not write: a forge we have never seen,
reached through a CLI we do not control. Its failures arrive as `AdapterError` carrying
the forge's own message, and uncaught that reached a reader as a Python traceback in
place of the refusal it already was — reported twice from walks over the released wheel,
from two different triggers: a remote that is not a forge URL, and a lane branch equal
to the base branch (where the message is gh's own refusal).

These witness the BOUNDARY rather than one trigger. `main()` is the console-script entry
point, so a traceback can only reach a user by escaping it, and catching it there covers
every adapter a user may plug in rather than one check per trigger.

TWO TRIGGERS, both driven without a network:

- the real `gh` on this machine, against a lane whose origin is a local path, which is
  the non-forge remote a first-run reader meets;
- a STUB `gh` first on PATH that refuses with the head-equals-base message, because the
  real one would have to accept an OWNER/REPO remote and reach the forge to say it. The
  stub stands in for the forge; the code path under test — gr2 invokes gh, reads the
  refusal, raises AdapterError — is the same one, and the message is the one measured.

The third test runs the same command through the installed console script, so the suite
would red if `main()` stopped being what the entry point calls.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli import app as gr2_app
from gr2.python_cli.syncops import run_sync

HEAD_EQUALS_BASE = 'head branch "main" is the same as base branch "main"'


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _bare_remote(tmp_path: Path, name: str) -> str:
    src = tmp_path / f"{name}-src"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "README.md").write_text(f"# {name}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(remote)], capture_output=True, check=True)
    return remote.as_uri()


@pytest.fixture
def lane_workspace(tmp_path: Path) -> Path:
    """A lane on a real lane branch, which is what `pr create` needs."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".grip").mkdir()
    (ws / ".grip" / "workspace_spec.toml").write_text(
        '[workspace]\nname = "boundary"\n\n'
        "[[repos]]\n"
        'name = "app"\n'
        'path = "app"\n'
        f'url = "{_bare_remote(tmp_path, "app")}"\n\n'
        "[[units]]\n"
        'name = "atlas"\n'
        'path = "agents/atlas/home"\n'
        'repos = ["app"]\n'
    )
    run_sync(ws)
    lane_proto.create_lane(
        SimpleNamespace(
            workspace_root=ws,
            owner_unit="atlas",
            lane_name="feat-auth",
            type="feature",
            repos="app",
            branch="feat/auth",
            default_commands=[],
            source="pytest",
        )
    )
    return ws


@pytest.fixture
def stub_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` first on PATH that refuses the way the forge did when head equals base."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\necho {HEAD_EQUALS_BASE!r} >&2\nexit 1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return gh


def _run_main(argv: list[str]) -> int:
    """Drive the console entry point in-process, exactly as the script does."""
    saved = sys.argv
    sys.argv = ["gr2", *argv]
    try:
        with pytest.raises(SystemExit) as exc:
            gr2_app.main()
        return int(exc.value.code or 0)
    finally:
        sys.argv = saved


def test_non_forge_remote_prints_one_sentence(
    lane_workspace: Path, capsys: pytest.CaptureFixture
) -> None:
    """Trigger 1: the remote is a local path, so the forge CLI cannot address a repo.

    Whether `gh` is installed or absent, the failure arrives as AdapterError; the
    assertion is on the BOUNDARY, not on which message the forge produced.
    """
    code = _run_main(["pr", "create", str(lane_workspace), "atlas", "feat-auth"])
    output = "".join(capsys.readouterr())
    assert code == 1, output
    assert "Traceback" not in output, output
    assert "gr2:" in output, output


def test_head_equals_base_prints_one_sentence(
    stub_gh, lane_workspace: Path, capsys: pytest.CaptureFixture
) -> None:
    """Trigger 2: the forge refuses because the head branch is the base branch.

    The message is the one measured on the released wheel; the stub stands in for
    the forge, which is the part of this path gr2 does not own.
    """
    code = _run_main(["pr", "create", str(lane_workspace), "atlas", "feat-auth"])
    output = "".join(capsys.readouterr())
    assert code == 1, output
    assert "Traceback" not in output, output
    assert HEAD_EQUALS_BASE in output, output


def test_module_entry_point_takes_the_same_boundary(lane_workspace: Path) -> None:
    """The `python -m gr2.python_cli` door must reach the SAME boundary.

    `main()` being guarded is not enough: `__main__.py` is a second way in, and it
    called `app()` directly, so the traceback still reached the user through a door
    the console script covers. `python -m gr2.python_cli` is not hypothetical — our
    own documentation uses it.

    EXIT CODE DOES NOT DISCRIMINATE HERE: Python's uncaught-exception exit is also 1,
    so both arms exit 1 and only the traceback separates them. This asserts the
    traceback's absence; a test asserting the code alone would stay green over the
    defect.
    """
    repo_root = Path(__file__).resolve().parents[2]
    # Assert WHICH gr2 we are about to run before running it: the overlay's editable
    # finder routes this module name to whatever desk it finds otherwise, so without
    # this the probe could measure another checkout and still look green.
    probe = subprocess.run(
        [sys.executable, "-c", "import gr2.python_cli.app as a; print(a.__file__)"],
        capture_output=True,
        text=True,
        cwd=str(lane_workspace.parent),
    )
    resolved = Path(probe.stdout.strip())
    assert str(resolved).startswith(str(repo_root)), (
        f"probe would measure {resolved}, not this repo at {repo_root}"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gr2.python_cli",
            "pr",
            "create",
            str(lane_workspace),
            "atlas",
            "feat-auth",
        ],
        capture_output=True,
        text=True,
        cwd=str(lane_workspace.parent),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output, output
    assert "gr2:" in output, output


def test_console_script_entry_point_takes_the_same_boundary(lane_workspace: Path) -> None:
    """The installed entry path, not just the function: the wiring itself.

    If `main()` stopped being what the entry point calls, the two tests above would
    stay green while a real user still met a traceback.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gr2.python_cli.app import main; main()",
            "pr",
            "create",
            str(lane_workspace),
            "atlas",
            "feat-auth",
        ],
        capture_output=True,
        text=True,
        cwd=str(lane_workspace.parent),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output, output
    assert "gr2:" in output, output
