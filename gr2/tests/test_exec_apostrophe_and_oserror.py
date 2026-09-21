"""TDD: the third exec/refusal branch (Stromus's carried findings).

1. `-- "./it's.sh"` — ONE token whose file name contains an apostrophe, the
   file present in each lane repo, caller standing outside the repos — runs
   on the released wheel. The v4 unbalanced-quote refusal catches it, because
   the lexer reads the apostrophe as a quote character and raises ValueError.
   Stromus's prescription (m_a9386472): on ValueError, refuse only when
   `shutil.which(single.split()[0])` resolves (the string names something the
   caller could run, so the quote is genuinely unbalanced); otherwise return
   the argument untouched, the released behaviour.

2. A directory a caller cannot enter (chmod 000) made the is_*_repo helpers
   raise OSError (PermissionError) out of the git probes: every single-repo
   verb crashed with a traceback. A directory that cannot be probed is not
   answerable as a repository — return False.

3. repos_under listed BARE repositories as "repositories found under it",
   but the refusal names repos the verb can run inside — and the verb needs
   a work tree. The listing is work trees only.

4. A per-repo command whose executable does not exist raised
   FileNotFoundError out of _exec_one and crashed the whole run; one repo's
   missing executable must be a per-repo failed status, not a crash.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli import gitops

runner = CliRunner()


def _lane_toml(doc: dict) -> str:
    repos = ", ".join(f'"{repo}"' for repo in doc["repos"])
    branch_map = ", ".join(f'"{k}" = "{v}"' for k, v in doc["branch_map"].items())
    return (
        f'lane_name = "{doc["lane_name"]}"\n'
        f'owner_unit = "{doc["owner_unit"]}"\n'
        f'lane_type = "{doc["lane_type"]}"\n'
        f"repos = [{repos}]\n"
        f"branch_map = {{{branch_map}}}\n"
        f'[exec_defaults]\nparallelism = "sequential"\nfail_fast = true\ncommands = []\n'
        f'\n[context]\nshared_roots = []\nprivate_roots = []\n'
    )


def _lane_base(workspace: Path, owner_unit: str, lane_name: str):
    return (
        workspace / ".grip" / "state" / "lanes" / owner_unit / lane_name
    )


class ExecApostropheBase(unittest.TestCase):
    """The minimal lane workspace, with an apostrophe script in each repo."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.workspace = self.tmp / "ws"
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        self.actor = "agent:test"

        lane_base = self.workspace / ".grip" / "state" / "lanes" / self.owner_unit / self.lane_name
        self.repos = ["repo-a", "repo-b"]
        for repo in self.repos:
            repo_dir = lane_base / "repos" / repo
            repo_dir.mkdir(parents=True, exist_ok=True)
            script = repo_dir / "it's.sh"
            script.write_text("#!/bin/sh\necho ran-apostrophe-ok\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        lane_doc = {
            "lane_name": self.lane_name,
            "owner_unit": self.owner_unit,
            "lane_type": "feature",
            "repos": self.repos,
            "branch_map": {repo: "feat/x" for repo in self.repos},
            "exec_defaults": {"parallelism": "sequential", "fail_fast": True, "commands": []},
            "context": {"shared_roots": [], "private_roots": []},
        }
        (lane_base / "lane.toml").write_text(_lane_toml(lane_doc))
        current = self.workspace / ".grip" / "state" / "current_lane"
        current.mkdir(parents=True, exist_ok=True)
        (current / f"{self.owner_unit}.json").write_text(
            json.dumps({"current": {"lane_name": self.lane_name}})
        )
        (self.workspace / ".grip" / "events").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestApostropheToken(ExecApostropheBase):
    def test_apostrophe_token_runs_per_repo_like_the_wheel(self) -> None:
        """`-- "./it's.sh"`: ONE token, apostrophe in the file name, the file
        present in each lane repo. The wheel runs it in both. The lexer
        raises ValueError on the apostrophe; the fix returns the argument
        untouched instead of refusing."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor, "--json", "--", "./it's.sh",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "unbalanced quote" not in result.output, result.output
        payload = json.loads(result.output)
        assert payload["command"] == ["./it's.sh"], payload["command"]
        statuses = {r["repo"]: r["status"] for r in payload["results"]}
        assert statuses == {"repo-a": "ok", "repo-b": "ok"}, statuses

    def test_unbalanced_quote_after_a_real_command_still_refuses(self) -> None:
        """'echo "hi': the first token IS runnable, so the quote is genuinely
        unbalanced and the one-sentence refusal stands (the wheel's answer
        was a FileNotFoundError traceback for the whole string as an
        executable name — strictly worse)."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor, "echo \"hi",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code != 0, result.output
        assert "unbalanced quote" in result.output, result.output


class TestUnenterableDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.root = self.tmp / "unit-home"
        self.root.mkdir()
        for name in ("alpha", "beta"):
            repo = self.root / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        self.locked = self.root / "locked"
        self.locked.mkdir()
        (self.locked / "keep").write_text("x")

    def tearDown(self):
        os.chmod(self.locked, 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helpers_answer_false_not_crash(self) -> None:
        """A chmod-000 directory: the git probes cannot run there (the
        subprocess cwd is unreadable) and must answer False, not raise."""
        os.chmod(self.locked, 0)
        try:
            assert gitops.is_git_repo(self.locked) is False
            assert gitops.is_bare_git_repo(self.locked) is False
            assert gitops.is_git_repository(self.locked) is False
        finally:
            os.chmod(self.locked, 0o755)

    def test_repos_under_skips_unenterable_and_lists_work_trees(self) -> None:
        """repos_under survives the unenterable child, and a bare repository
        child is NOT in the listing: the refusal names repos the verb (which
        needs a work tree) could actually run inside."""
        bare = subprocess.run(
            ["git", "init", "-q", "--bare", str(self.root / "bare-upstream")],
            check=True, capture_output=True, text=True,
        )
        assert bare.returncode == 0
        os.chmod(self.locked, 0)
        try:
            found = gitops.repos_under(self.root)
        finally:
            os.chmod(self.locked, 0o755)
        names = {p.name for p in found}
        assert "alpha" in names and "beta" in names, names
        assert "bare-upstream" not in names, (
            f"a bare repository is not a work tree; the refusal must not offer it:\n{names}"
        )
        assert "locked" not in names, names

    def test_require_git_repo_refusal_is_one_sentence_with_unenterable_child(self) -> None:
        """branch from a unit home holding two work trees, a bare upstream,
        and a chmod-000 directory: one sentence, listing the work trees, no
        traceback."""
        os.chmod(self.locked, 0)
        try:
            result = runner.invoke(app, ["branch", "feat/x", "--repo-path", str(self.root)], catch_exceptions=False)
        finally:
            os.chmod(self.locked, 0o755)
        assert "Traceback" not in result.output, result.output
        assert "PermissionError" not in result.output, result.output
        assert result.exit_code == 1, result.output
        assert "alpha" in result.output and "beta" in result.output, result.output


class TestExecMissingExecutable(ExecApostropheBase):
    def test_missing_executable_is_a_failed_status_not_a_crash(self) -> None:
        """A per-repo command whose executable does not exist raises
        FileNotFoundError out of _exec_one; one repo's missing executable is
        a failed status for that repo, never a crash of the whole run."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor, "--json", "--",
                "definitely-not-a-real-executable-9x7q", "arg1",
            ],
        )
        assert "Traceback" not in result.output, result.output
        payload = json.loads(result.output)
        for row in payload["results"]:
            assert row["status"] == "failed", row
            assert row["returncode"] is None, row
            assert row["stderr"], row
        assert payload.get("status") == "failed", payload.get("status")

class TestGitMissing(unittest.TestCase):
    """Stromus's v1 R2 block (m_9341d8d1): `except OSError` was wider than the
    chmod-000 state. With git ABSENT from PATH (a fresh container), the git
    probes raise FileNotFoundError for the executable; answering False made
    every refusal say "<dir> is not a git repository (no repositories found
    under it)" — a false sentence about a machine that simply has no git.
    The catch narrows to PermissionError (the unenterable-directory answer),
    and a missing git names git in one sentence."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.root = self.tmp / "unit-home"
        self.root.mkdir()
        repo = self.root / "alpha"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_git_names_git_not_a_false_repo_sentence(self) -> None:
        """env -i, PATH without git: the CLI line must name git, never claim
        the directory is not a git repository."""
        env = {"PATH": "/usr/bin/nonexistent-dir-for-test", "HOME": str(self.tmp)}
        result = subprocess.run(
            [f"{Path(sys.executable).parent / 'gr2'}", "branch", "feat/x",
             "--repo-path", str(self.root)],
            env=env, capture_output=True, text=True,
        )
        combined = result.stdout + result.stderr
        assert "git is not on PATH" in combined, combined
        assert "is not a git repository" not in combined, combined

    def test_helpers_do_not_answer_false_for_a_missing_executable(self) -> None:
        """The FileNotFoundError for a missing git executable propagates out of
        the helpers as GitMissingError; it never becomes a False answer."""
        from unittest import mock

        def spawnless(args, **kwargs):
            # The shape of a missing git at subprocess spawn time: the
            # FileNotFoundError names the executable.
            raise FileNotFoundError(2, "No such file or directory", "git")

        with mock.patch.object(gitops.subprocess, "run", spawnless):
            try:
                gitops.is_git_repo(self.root)
            except gitops.GitMissingError:
                pass
            else:
                raise AssertionError(
                    "a missing git must raise GitMissingError, not answer False"
                )
