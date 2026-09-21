"""TDD: two stranger findings from Fathom's lane sounding (r2-lane-stranger-
sounding-2026-09-20) and the materialize plan gate.

1. `gr2 exec run <ws> <unit> --actor … 'git rev-parse --show-toplevel'` — the
   command as ONE quoted string, which is what people type — passed the whole
   string to subprocess as the executable and died in a FileNotFoundError
   traceback. The fix shlex-splits a SINGLE argument, because the intent is
   unambiguous (a command line) and the split lets the stranger's natural
   spelling work on the first try; a refusal would teach the `--` form at the
   cost of a failed round-trip. When the single string carries shell operators
   the stranger is expecting shell semantics gr2 does not provide (exec runs
   no shell), so THAT case refuses and names the `--` form. The multi-argument
   `--` form is untouched.

2. `gr2 workspace materialize` without `--yes` refuses at more than 3
   operations and prints nothing about what those operations ARE. The refusal
   now carries the plan.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()


def _lane_toml(doc: dict) -> str:
    """Render the minimal lane.toml the lane reader loads (TOML, not JSON)."""
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


class ExecSingleStringTestBase(unittest.TestCase):
    """The same minimal lane workspace test_execops builds, via the CLI."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "ws"
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        self.actor = "agent:test"

        lane_base = self.workspace / ".grip" / "state" / "lanes" / self.owner_unit / self.lane_name
        self.repos = ["repo-a", "repo-b"]
        for repo in self.repos:
            (lane_base / "repos" / repo).mkdir(parents=True, exist_ok=True)

        lane_doc = {
            "lane_name": self.lane_name,
            "owner_unit": self.owner_unit,
            "lane_type": "feature",
            "repos": self.repos,
            "branch_map": {repo: "feat/x" for repo in self.repos},
            "exec_defaults": {
                "parallelism": "sequential",
                "fail_fast": True,
                "commands": [],
            },
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


class TestExecSingleQuotedString(ExecSingleStringTestBase):
    def test_single_quoted_command_is_split_and_runs(self) -> None:
        """The natural single-quoted form: split it and run the tokens. The
        receipt must show the split command, not a FileNotFoundError for an
        executable named 'git rev-parse --show-toplevel'."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor, "--json",
                "git rev-parse --show-toplevel",
            ],
        )
        assert "FileNotFoundError" not in result.output, (
            f"the single-string form must not crash:\\n{result.output}"
        )
        payload = json.loads(result.output)
        assert payload["command"] == ["git", "rev-parse", "--show-toplevel"], (
            f"the command must be split into tokens:\\n{payload['command']}"
        )

    def test_single_quoted_command_with_shell_operators_refuses(self) -> None:
        """One quoted string WITH shell operators: the stranger expects shell
        semantics exec does not provide, so this refuses and names the `--`
        form — it must not silently split a pipeline into tokens."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor,
                "git rev-parse --show-toplevel && echo done",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code != 0, result.output
        assert "--" in result.output, (
            f"the refusal must name the `--` token form:\\n{result.output}"
        )

    def test_multi_token_form_is_untouched(self) -> None:
        """The documented `--` form keeps its exact tokens."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", self.actor, "--json",
                "--", "git", "rev-parse", "--show-toplevel",
            ],
            catch_exceptions=False,
        )
        payload = json.loads(result.output)
        assert payload["command"] == ["git", "rev-parse", "--show-toplevel"]


class TestMaterializePlanOnRefusal(unittest.TestCase):
    def test_more_than_three_operations_prints_the_plan(self) -> None:
        """The >3-operation refusal now carries the plan it is refusing."""
        ws = Path(__import__("tempfile").mkdtemp())
        try:
            for repo in ("alpha", "beta"):
                (ws / repo).mkdir()
                subprocess.run(["git", "init", "-q", "-b", "main", str(ws / repo)], check=True)
                subprocess.run(["git", "-C", str(ws / repo), "config", "user.name", "T"], check=True)
                subprocess.run(["git", "-C", str(ws / repo), "config", "user.email", "t@t"], check=True)
                (ws / repo / "README.md").write_text("# x")
                subprocess.run(["git", "-C", str(ws / repo), "add", "README.md"], check=True)
                subprocess.run(["git", "-C", str(ws / repo), "commit", "-qm", "init"], check=True)
                subprocess.run(
                    ["git", "-C", str(ws / repo), "remote", "add", "origin", f"https://example.invalid/{repo}.git"],
                    check=True,
                )
            init = runner.invoke(app, ["workspace", "init", str(ws)])
            assert init.exit_code == 0, init.output

            result = runner.invoke(app, ["workspace", "materialize", str(ws)])
            assert result.exit_code != 0, result.output
            assert "rerun with --yes" in result.output, (
                f"the refusal must keep the fix instruction:\\n{result.output}"
            )
            assert "ExecutionPlan" in result.output, (
                f"the refusal must print the plan it refuses:\\n{result.output}"
            )
            assert "converge_unit_repos" in result.output, (
                f"the plan must show the actual operations:\\n{result.output}"
            )
        finally:
            shutil.rmtree(ws, ignore_errors=True)

class TestSingleTokenIsNeverSplit(unittest.TestCase):
    """The gate's E4/E5 (measured against the released wheel as base): ONE
    legitimate token is also exactly one argument. An executable under a
    directory with a space runs on base and died in a FileNotFoundError on the
    first v1 split; a Windows-style backslash token reaches subprocess intact
    on base and lost its backslashes to POSIX-mode shlex on v1. The split now
    requires whitespace AND not-runnable, and a no-whitespace token is never
    touched."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "ws"
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        lane_base = self.workspace / ".grip" / "state" / "lanes" / self.owner_unit / self.lane_name
        self.repos = ["repo-a"]
        for repo in self.repos:
            (lane_base / "repos" / repo).mkdir(parents=True, exist_ok=True)
        (self.workspace / ".grip" / "state" / "current_lane").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".grip" / "state" / "current_lane" / f"{self.owner_unit}.json").write_text(
            json.dumps({"current": {"lane_name": self.lane_name}})
        )
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
        (self.workspace / ".grip" / "events").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_executable_path_with_spaces_runs_as_one_token(self) -> None:
        """An executable whose path has spaces, passed as ONE token after --:
        it runs, and the receipt's command is the literal path — not the
        v1 split's FileNotFoundError on the first word."""
        tools = self.tmp / "my tools"
        tools.mkdir()
        script = tools / "say hi.sh"
        script.write_text("#!/bin/sh\necho SPACE_PATH_OK\n")
        script.chmod(0o755)

        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", "agent:test", "--json", "--", str(script),
            ],
        )
        assert "FileNotFoundError" not in result.output, (
            f"a runnable single token must not be split:\\n{result.output}"
        )
        payload = json.loads(result.output)
        assert payload["command"] == [str(script)], (
            f"the receipt must carry the literal path:\\n{payload['command']}"
        )

    def test_no_whitespace_backslash_token_reaches_run_exec_intact(self) -> None:
        """A Windows-style one-token path (no whitespace) is never split and
        never operator-checked: it reaches run_exec byte-for-byte, backslashes
        included — POSIX-mode shlex would eat them. No Windows needed: the
        witness is the token list."""
        from gr2.python_cli.app import normalize_single_command_arg

        token = "C:\\tools\\gradlew.bat"
        assert normalize_single_command_arg(["--", token] if False else [token]) == [token]
        # And a token that also names nothing runnable is STILL untouched:
        # no-whitespace means never split.
        assert normalize_single_command_arg([token, "extra"]) == [token, "extra"]

    def test_split_still_applies_to_the_natural_quoted_form(self) -> None:
        """The guard must not break the original finding's fix: a whitespace
        string that names nothing runnable still splits."""
        from gr2.python_cli.app import normalize_single_command_arg

        assert normalize_single_command_arg(["git rev-parse --show-toplevel"]) == [
            "git", "rev-parse", "--show-toplevel"
        ]

class TestOperatorAndEmptinessSeams(unittest.TestCase):
    """Sentinel's R1 on exec-plan v1, two seams: (1) the operator check ran on
    the RAW string, so an operator inside a quotation span ('fix: a|b') was
    refused with no shell intent — it is now judged on the split tokens, where
    a quoted span is part of a longer token; (2) an empty/whitespace-only
    single string split to nothing and reached run_exec as an IndexError —
    the missing-command guard now repeats after the split."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "ws"
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        lane_base = self.workspace / ".grip" / "state" / "lanes" / self.owner_unit / self.lane_name
        for repo in ("repo-a",):
            (lane_base / "repos" / repo).mkdir(parents=True, exist_ok=True)
        (self.workspace / ".grip" / "state" / "current_lane").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".grip" / "state" / "current_lane" / f"{self.owner_unit}.json").write_text(
            json.dumps({"current": {"lane_name": self.lane_name}})
        )
        lane_doc = {
            "lane_name": self.lane_name,
            "owner_unit": self.owner_unit,
            "lane_type": "feature",
            "repos": ["repo-a"],
            "branch_map": {"repo-a": "feat/x"},
            "exec_defaults": {"parallelism": "sequential", "fail_fast": True, "commands": []},
            "context": {"shared_roots": [], "private_roots": []},
        }
        (lane_base / "lane.toml").write_text(_lane_toml(lane_doc))
        (self.workspace / ".grip" / "events").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_operator_inside_quotes_is_not_refused(self) -> None:
        """A quoted pipe inside a commit message has no shell intent: the split
        tokens carry it inside one token, so the command proceeds."""
        from gr2.python_cli.app import normalize_single_command_arg

        tokens = normalize_single_command_arg(["git commit -m 'fix: a|b'"])
        assert tokens == ["git", "commit", "-m", "fix: a|b"], tokens
        # The refusal class only fires for a token that IS an operator.
        import pytest
        from typer import BadParameter

        with pytest.raises(BadParameter):
            normalize_single_command_arg(["git rev-parse --show-toplevel && echo done"])

    def test_empty_and_whitespace_single_strings_refuse(self) -> None:
        for quoted in ("", "   "):
            result = runner.invoke(
                app,
                [
                    "exec", "run", str(self.workspace), self.owner_unit,
                    "--actor", "agent:test", quoted,
                ],
            )
            assert result.exit_code == 2, f"{quoted!r}: {result.output}"
            assert "missing command to run" in result.output, (
                f"{quoted!r} must refuse as missing, not IndexError:\\n{result.output}"
            )
            assert "Traceback" not in result.output, result.output
            assert "IndexError" not in result.output, result.output

class TestRelativeTokenAndGluedOperators(unittest.TestCase):
    """The gate's second round on the normalize seam, measured against the
    released wheel as base:

    - './rel tool.sh' is ONE token, a relative executable with spaces that
      lives in EACH REPO's cwd (the caller stands outside the repos). The v2
      split kept it split because guard (b) tested existence against the
      caller's cwd — the wheel runs it. The split is now kept only when the
      FIRST TOKEN names something runnable (shutil.which); otherwise the
      argument is returned untouched, the released behaviour, which subsumes
      the v2 existence guard.
    - 'echo a>b' glued its operator to a word, and token EQUALITY never saw
      it: v1 refused it, v2 passed it silently printing the literal 'a>b'.
      The lexer now runs with punctuation_chars=True so glued operators
      separate into tokens the check can see.
    - An unbalanced quote raises ValueError from the lexer; caught and
      refused in one sentence (an uncaught ValueError is a traceback).
    """

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "ws"
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        lane_base = self.workspace / ".grip" / "state" / "lanes" / self.owner_unit / self.lane_name
        self.repos = ["repo-a", "repo-b"]
        for repo in self.repos:
            repo_dir = lane_base / "repos" / repo
            repo_dir.mkdir(parents=True, exist_ok=True)
            script = repo_dir / "rel tool.sh"
            script.write_text("#!/bin/sh\necho REL_TOKEN_OK\n")
            script.chmod(0o755)
        (self.workspace / ".grip" / "state" / "current_lane").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".grip" / "state" / "current_lane" / f"{self.owner_unit}.json").write_text(
            json.dumps({"current": {"lane_name": self.lane_name}})
        )
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
        (self.workspace / ".grip" / "events").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relative_executable_with_spaces_runs_per_repo(self) -> None:
        """Caller outside the repos, one relative token with spaces that lives
        in each repo: runs in both, receipt carries the literal token — the
        released behaviour the v2 split broke."""
        import os as _os
        caller = self.tmp / "caller"
        caller.mkdir()
        prev = _os.getcwd()
        _os.chdir(caller)
        try:
            result = runner.invoke(
                app,
                [
                    "exec", "run", str(self.workspace), self.owner_unit,
                    "--actor", "agent:test", "--json", "--", "./rel tool.sh",
                ],
            )
        finally:
            _os.chdir(prev)
        assert "FileNotFoundError" not in result.output, (
            f"the relative token must reach run_exec whole:\\n{result.output}"
        )
        payload = json.loads(result.output)
        assert payload["command"] == ["./rel tool.sh"], (
            f"the receipt must carry the literal token:\\n{payload['command']}"
        )
        rendered = json.dumps(payload)
        assert "REL_TOKEN_OK" in rendered or result.output.count("REL_TOKEN_OK") >= 1, (
            f"the script must have run in the repos:\\n{rendered}"
        )

    def test_glued_operator_refuses(self) -> None:
        """'echo a>b': the lexer separates the glued operator, which(echo)
        resolves, and the operator check refuses — not a silent pass printing
        the literal 'a>b'."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", "agent:test", "echo a>b",
            ],
        )
        assert result.exit_code != 0, result.output
        assert "shell" in result.output and "--" in result.output, (
            f"the glued operator must refuse with the -- form:\\n{result.output}"
        )

    def test_unbalanced_quote_refuses_in_one_sentence(self) -> None:
        """'echo "hi': the lexer's ValueError is caught and refused; no
        traceback (the v2 seam)."""
        result = runner.invoke(
            app,
            [
                "exec", "run", str(self.workspace), self.owner_unit,
                "--actor", "agent:test", "echo \"hi",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "unbalanced quote" in result.output, result.output
        assert "Traceback" not in result.output, result.output