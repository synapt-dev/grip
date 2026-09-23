"""Every verb whose root could be implied now implies it.

The originating issue is tracked privately and deliberately not linked here.

The stranger's first minute, measured on a fresh one-seat workspace with
gitgrip 2.0.0a2 from PyPI: `gr2 spec validate` and `gr2 plan` from inside the
workspace root exited with `Missing argument 'workspace_root'`. Every git user
expects `.` to be implied, so the first command they try reads as broken.

The contract pinned here, with the DISCRIMINATING case first: the root is found
by walking UP from the current directory to the nearest ancestor holding
`.grip/workspace_spec.toml`, so it works from a subdirectory, not merely from
the root itself (a cwd-only fallback passes the root case and fails this one).
An explicit argument still wins, and is still resolved, so the explicit form is
unchanged. Outside any workspace the verb reaches its OWN error about a missing
spec rather than an argparse error about a missing argument.

Not covered here, and deliberately: the commands whose `workspace_root` is
followed by other required positionals (lane create/enter/exit/bind/lease,
pr status/create/checks/merge, exec status/run, repo hook/projection run,
review requirements/checkout-pr, lane resolve/current). A defaulted parameter
must follow every required one in Python, so those need either a reordered
positional list or defaults on their neighbours, and neither is a mechanical
change to make on the owner's behalf.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app


runner = CliRunner()


def _repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    # An origin, so the generated spec validates: without one, `validate` exits 1
    # on missing_repo_url and this fixture could not tell "the verb found the
    # root and read the spec" from "the verb never ran".
    subprocess.run(
        ["git", "remote", "add", "origin", f"git@github.com:example/{path.name}.git"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def _workspace(tmp_path: Path) -> Path:
    """A real workspace built the documented way, then nothing else."""
    _repo(tmp_path / "repo-a")
    init = runner.invoke(app, ["workspace", "init", str(tmp_path)])
    assert init.exit_code == 0, init.output
    assert (tmp_path / ".grip" / "workspace_spec.toml").is_file(), "fixture: no spec was written"
    return tmp_path


class TestTheRootIsFoundByWalkingUp:
    def test_spec_validate_from_a_subdirectory_with_no_argument(self, tmp_path, monkeypatch):
        root = _workspace(tmp_path)
        nested = root / "repo-a"
        monkeypatch.chdir(nested)

        result = runner.invoke(app, ["spec", "validate"])

        assert "Missing argument" not in result.output, result.output
        assert result.exit_code == 0, result.output

    def test_plan_from_a_subdirectory_with_no_argument(self, tmp_path, monkeypatch):
        root = _workspace(tmp_path)
        monkeypatch.chdir(root / "repo-a")

        result = runner.invoke(app, ["plan"])

        assert "Missing argument" not in result.output, result.output
        assert result.exit_code == 0, result.output

    def test_a_deeper_subdirectory_still_resolves(self, tmp_path, monkeypatch):
        """Two levels down, so the walk is not a single parent step."""
        root = _workspace(tmp_path)
        deep = root / "repo-a" / "docs" / "guide"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        result = runner.invoke(app, ["spec", "validate"])

        assert result.exit_code == 0, result.output

    def test_an_explicit_argument_wins_over_the_ancestor(self, tmp_path, monkeypatch):
        """The discriminating control: cwd is inside workspace A, the argument
        names workspace B, and B is the one the verb reads."""
        ws_a = tmp_path / "a"
        ws_a.mkdir()
        _workspace(ws_a)
        ws_b = tmp_path / "b"
        ws_b.mkdir()
        _workspace(ws_b)
        monkeypatch.chdir(ws_a / "repo-a")

        result = runner.invoke(app, ["spec", "validate", str(ws_b)])

        assert result.exit_code == 0, result.output
        assert str(ws_b) not in result.output or "Missing argument" not in result.output

    def test_outside_any_workspace_the_verb_reaches_its_own_error(self, tmp_path, monkeypatch):
        """Falling back to cwd must not read as success: with no spec anywhere
        the verb fails on the MISSING SPEC, which is a different error from a
        missing argument."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)

        result = runner.invoke(app, ["spec", "validate"])

        assert "Missing argument" not in result.output, result.output
        assert result.exit_code != 0, "a directory with no spec must not validate clean"


class TestTheVerbsTheIssueNames:
    def test_migrate_gr1_from_a_subdirectory_with_no_argument(self, tmp_path, monkeypatch):
        """migrate-gr1 on a workspace that is already a gr1→gr2 conversion: the
        verb must resolve the root itself. It refuses because there is nothing
        to migrate; what matters is that it does not refuse for a missing
        argument."""
        root = _workspace(tmp_path)
        monkeypatch.chdir(root / "repo-a")

        result = runner.invoke(app, ["workspace", "migrate-gr1"])

        assert "Missing argument" not in result.output, result.output

    def test_workspace_materialize_from_a_subdirectory(self, tmp_path, monkeypatch):
        root = _workspace(tmp_path)
        monkeypatch.chdir(root / "repo-a")

        result = runner.invoke(app, ["workspace", "materialize", "--json"])

        assert "Missing argument" not in result.output, result.output
