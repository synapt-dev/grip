"""TDD tests for grip + config CLI wiring.

Tests the typer CLI layer, not the library functions (those are tested
in test_grip_snapshot.py, test_config_overlay.py, test_grip_hardening.py).

Focus: argument parsing, exit codes, JSON output format, error messages.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer
from tests.conftest import make_cli_runner

from gr2.python_cli.gitops import git
from gr2.python_cli.grip_cli import config_cli_app, grip_app

app = typer.Typer()
app.add_typer(grip_app, name="store")
app.add_typer(config_cli_app, name="config")

runner = make_cli_runner()

SAMPLE_TOML = """\
[spawn]
session_name = "synapt"
channel = "dev"

[agents.opus]
role = "CEO / product design"
model = "claude-opus-4-6"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, name: str = "test") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@test.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text(f"# {name}\n")
    git(path, "add", ".")
    git(path, "commit", "-m", f"init {name}")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_repo(ws / "recall", name="recall")
    git(ws / "recall", "remote", "add", "origin", "https://github.com/synapt-dev/recall")
    config_dir = ws / "config_files"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()
    return ws


@pytest.fixture
def native_workspace(tmp_path: Path) -> Path:
    """A workspace whose member is COVERED by its upstream, for the native-verb rows.

    ⚠ WHY THIS EXISTS AND `workspace` CANNOT BE IT (measured 2026-09-28, step 3 v4): the
    native verbs check section 5a coverage -- the member's HEAD must be an ancestor of its
    upstream -- and refuse at 3 otherwise. The `workspace` fixture above points `recall` at a
    FICTIONAL github.com URL, so every native write there refuses with

        recall pin <sha> is not on origin/main; push it first        (rc 3)

    measured on the same rows: rc 3 on the fictional remote, rc 0 once the member pushes to a
    local bare remote instead. This fixture is that local remote, and it is separate from
    `workspace` so the init/config rows that do not touch a remote keep the fixture they had.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    member = ws / "recall"
    member.mkdir()
    _init_repo(member, name="recall")
    bare = tmp_path / "recall.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], capture_output=True, check=True)
    git(member, "remote", "add", "origin", str(bare))
    git(member, "push", "-q", "-u", "origin", "HEAD:refs/heads/main")
    config_dir = ws / "config_files"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()
    return ws


# ---------------------------------------------------------------------------
# gr grip init
# ---------------------------------------------------------------------------


class TestGripInitCLI:
    def test_init_succeeds(self, workspace: Path) -> None:
        result = runner.invoke(app, ["store", "init", str(workspace)])
        assert result.exit_code == 0

    def test_init_json_output(self, workspace: Path) -> None:
        result = runner.invoke(app, ["store", "init", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "initialized"

    def test_init_idempotent(self, workspace: Path) -> None:
        runner.invoke(app, ["store", "init", str(workspace)])
        result = runner.invoke(app, ["store", "init", str(workspace)])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# gr grip snapshot
# ---------------------------------------------------------------------------


class TestGripSnapshotCLI:
    """`snapshot` is a HIDDEN ALIAS of `store commit` (design section 5), so its rows now
    drive commit's signature and commit's JSON shape.

    ⚠ REWRITTEN NATIVELY 2026-09-28 (step 3 v4), and the reason is the class this file caused:
    every row here used to pass a native_workspace POSITIONAL to a ported verb. The port removed the
    positional (the verbs act on cwd), so typer refused the call at the PARSER -- exit 2 before
    any store code ran -- and eleven rows went red for a reason that had nothing to do with what
    they were asserting. Two rows whose SUBJECT died with the alpha writer are RETIRED rather
    than rewritten: `test_snapshot_with_type_and_sprint` (--type/--sprint are commit flags that
    do not exist) and `test_snapshot_multiple_repos` (--repos is gone; member selection is the
    store's own members, covered by the store witnesses).
    """

    def test_snapshot_succeeds(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        result = runner.invoke(app, ["store", "commit", "-m", "first root"])
        assert result.exit_code == 0, result.stdout

    def test_snapshot_json_output(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        result = runner.invoke(app, ["store", "commit", "-m", "first root", "--json"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert data["status"] == "committed"
        assert len(data["root_commit"]) >= 40

    def test_snapshot_with_message(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        result = runner.invoke(app, ["store", "commit", "-m", "Sprint 27 ceremony", "--json"])
        assert result.exit_code == 0, result.stdout
        # the message is the store commit's own message, so the LOG is where it can be checked
        log = runner.invoke(app, ["store", "log", "--json"])
        entry = json.loads(log.stdout)["entries"][0]
        assert entry["message"] == "Sprint 27 ceremony"

    def test_snapshot_without_init_fails(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The native refusal, not the parser's.

        This row used to pass for the WRONG reason once the positional was removed: typer
        rejected the extra argument and the non-zero exit satisfied `!= 0` without any store
        code running. Without the positional it exercises what it was written for.
        """
        monkeypatch.chdir(native_workspace)
        result = runner.invoke(app, ["store", "commit", "-m", "no store here"])
        assert result.exit_code != 0
        assert "Traceback" not in (result.stderr or "")


# ---------------------------------------------------------------------------
# gr grip log
# ---------------------------------------------------------------------------


class TestGripLogCLI:
    """`log` is the root's own history with its pins (design section 5), so it acts on cwd and
    takes no workspace positional. REWRITTEN NATIVELY 2026-09-28 (step 3 v4): the rows below
    drive the native shape and assert the native entry fields (`commit`/`message`), not the
    alpha index's (`id`).
    """

    def test_log_before_any_root_commit_refuses_at_5(
        self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The native contract: no root commit is a REFUSAL at 5, not an empty list.

        RENAMED AND REWRITTEN in v4. The alpha verb answered an empty index with `entries: []`;
        the ported verb cannot measure a history that does not exist yet, so it refuses and
        names the command to run -- the guard v3 added for exactly this state. Keeping
        `entries == []` would be a row asserting a behaviour that no longer exists.
        """
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        result = runner.invoke(app, ["store", "log", "--json"])
        assert result.exit_code == 5, result.stdout
        assert "run store commit" in (result.stderr or "")

    def test_log_after_snapshot(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        runner.invoke(app, ["store", "commit", "-m", "test snap"])
        result = runner.invoke(app, ["store", "log", "--json"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert len(data["entries"]) == 1
        assert "test snap" in data["entries"][0]["message"]

    def test_log_max_count(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        for i in range(3):
            (native_workspace / "recall" / f"f{i}.txt").write_text(str(i))
            git(native_workspace / "recall", "add", ".")
            git(native_workspace / "recall", "commit", "-m", f"c{i}")
            # the member must be PUSHED before the store can pin it: section 5a coverage refuses
            # at 3 otherwise (measured: the second snapshot exits 3 with "pin ... is not on
            # origin/main; push it first" when the commit is local-only).
            git(native_workspace / "recall", "push", "-q", "origin", "HEAD:refs/heads/main")
            made = runner.invoke(app, ["store", "commit", "-m", f"root {i}", "--json"])
            assert made.exit_code == 0, made.stdout
        result = runner.invoke(app, ["store", "log", "--max-count", "2", "--json"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert len(data["entries"]) == 2

    def test_log_without_init_fails(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        result = runner.invoke(app, ["store", "log"])
        assert result.exit_code != 0
        assert "Traceback" not in (result.stderr or "")


# ---------------------------------------------------------------------------
# gr grip diff
# ---------------------------------------------------------------------------


class TestGripDiffCLI:
    """`diff` is pin changes between two ROOT COMMITS, and it acts on cwd. REWRITTEN NATIVELY
    2026-09-28 (step 3 v4): the old row passed a workspace positional and read `data["changed"]`,
    the alpha index's shape. The native payload names `ref_a`, `ref_b` and one entry per member.
    """

    def test_diff_json(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        r1 = runner.invoke(app, ["store", "commit", "-m", "root one", "--json"])
        assert r1.exit_code == 0, r1.stdout
        root1 = json.loads(r1.stdout)["root_commit"]

        (native_workspace / "recall" / "new.txt").write_text("x")
        git(native_workspace / "recall", "add", ".")
        git(native_workspace / "recall", "commit", "-m", "change")
        # coverage again: an unpushed member pin refuses at 3, so the change must be on origin
        git(native_workspace / "recall", "push", "-q", "origin", "HEAD:refs/heads/main")

        r2 = runner.invoke(app, ["store", "commit", "-m", "root two", "--json"])
        assert r2.exit_code == 0, r2.stdout
        root2 = json.loads(r2.stdout)["root_commit"]

        result = runner.invoke(app, ["store", "diff", root1, root2, "--json"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert data["ref_a"] == root1 and data["ref_b"] == root2
        changed = [m["name"] for m in data["members"] if m["changed"]]
        assert "recall" in changed, data

    def test_diff_without_init_fails(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        result = runner.invoke(app, ["store", "diff", "abc", "def"])
        assert result.exit_code != 0
        assert "Traceback" not in (result.stderr or "")


# ---------------------------------------------------------------------------
# gr grip checkout
# ---------------------------------------------------------------------------


class TestGripCheckoutCLI:
    """`checkout` materializes the members at a root commit, and it acts on cwd. REWRITTEN
    NATIVELY 2026-09-28 (step 3 v4): the old row read `data["repos"]`, the alpha index's shape;
    the native payload reports `root_commit` and one entry per member.
    """

    def test_checkout_json(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        runner.invoke(app, ["store", "init"])
        r1 = runner.invoke(app, ["store", "commit", "-m", "root one", "--json"])
        assert r1.exit_code == 0, r1.stdout
        root = json.loads(r1.stdout)["root_commit"]

        result = runner.invoke(app, ["store", "checkout", root, "--json"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert data["root_commit"] == root
        assert "recall" in [m["name"] for m in data["members"]], data

    def test_checkout_without_init_fails(self, native_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(native_workspace)
        result = runner.invoke(app, ["store", "checkout", "HEAD"])
        assert result.exit_code != 0
        assert "Traceback" not in (result.stderr or "")


# ---------------------------------------------------------------------------
# gr config apply
# ---------------------------------------------------------------------------


class TestConfigApplyCLI:
    def test_apply_succeeds(self, workspace: Path) -> None:
        result = runner.invoke(
            app,
            [
                "config",
                "apply",
                str(workspace / "config_files" / "agents.toml"),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
            ],
        )
        assert result.exit_code == 0

    def test_apply_json_output(self, workspace: Path) -> None:
        result = runner.invoke(
            app,
            [
                "config",
                "apply",
                str(workspace / "config_files" / "agents.toml"),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
                "--json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "agents" in data
        assert "spawn" in data


# ---------------------------------------------------------------------------
# gr config show
# ---------------------------------------------------------------------------


class TestConfigShowCLI:
    def _apply_first(self, workspace: Path) -> None:
        runner.invoke(
            app,
            [
                "config",
                "apply",
                str(workspace / "config_files" / "agents.toml"),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
            ],
        )

    def test_show_full(self, workspace: Path) -> None:
        self._apply_first(workspace)
        result = runner.invoke(
            app,
            [
                "config",
                "show",
                str(workspace / "config_files" / "agents.toml"),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
                "--json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "agents" in data

    def test_show_with_key(self, workspace: Path) -> None:
        self._apply_first(workspace)
        result = runner.invoke(
            app,
            [
                "config",
                "show",
                str(workspace / "config_files" / "agents.toml"),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
                "--key",
                "agents.opus.role",
                "--json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["value"] == "CEO / product design"

    def test_show_strict_stale(self, workspace: Path) -> None:
        self._apply_first(workspace)
        base = workspace / "config_files" / "agents.toml"
        base.write_text(SAMPLE_TOML + '\n[agents.new]\nrole = "new"\n')
        result = runner.invoke(
            app,
            [
                "config",
                "show",
                str(base),
                "--overlay-dir",
                str(workspace / "config_files" / "overlay"),
                "--strict",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gr config restore
# ---------------------------------------------------------------------------


class TestConfigRestoreCLI:
    """RETIRED 2026-09-28 (step 3 v4), and this CORRECTS the ruling's assumption that config
    restore's subject survives. It does not, for two independent reasons, both measured:

    1. `config restore` still reaches into the REMOVED alpha store. `config.py:config_restore`
       delegates to `_grip_git(workspace, ...)`, which is `git(workspace / ".grip", ...)`
       (`grip.py:949`). The native store deliberately has no `.grip` repo (section 1), so the
       subprocess raises
           FileNotFoundError: [Errno 2] No such file or directory: '<ws>/.grip'
       and the verb exits 1 through click's exception path.
    2. Even with the cwd fixed it would restore nothing: the native root commit's tree is
       `.gitignore`, `grip.toml`, the members -- there is NO `config/` subtree, because
       `store commit` records member PINS and the alpha snapshot is what used to record the
       config overlay.

    So the row is retired rather than rewritten: rewriting it would assert `exit_code == 0` for
    a verb that returns zero restored files, which is a row that passes while testing nothing.
    The finding is named in the v4 PR body and on #dev; the config surface's own port (the
    `.grip` path family across app.py/config.py/consent.py) is a separate lane, not this one.

    The verb keeps its `(workspace_root, ref)` signature, so nothing here is about its argument
    shape -- it is about the STORE it reads.
    """
