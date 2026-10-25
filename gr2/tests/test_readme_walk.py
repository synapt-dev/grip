"""The README's "First five minutes" says every command in it was run, in order. This runs them.

The commands are read from README.md itself, so the README and this test cannot drift apart: a command
that refuses, or an output block that no longer matches what the command prints, turns this file red.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

README = Path(__file__).resolve().parents[1] / "README.md"
runner = CliRunner()

# A walk line that refuses today, by a defect the README names under "Not there yet": verb -> the refusal it prints.
# When the defect is fixed this row goes red, and the README's note and this entry go together.
KNOWN_REFUSALS: dict[str, str] = {}


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


def _run_block(block: str, root: Path, monkeypatch, out: dict[str, str]) -> int:
    """Each `gr2 ...` line of a README bash block, with ~/ws as the fixture root; stdout per verb goes into ``out``.
    Returns how many gr2 lines ran."""
    ran = 0
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
        known = KNOWN_REFUSALS.get(" ".join(argv[1:3]))
        if known:
            assert result.exit_code != 0 and known in result.output, (
                f"README line {line!r} no longer refuses with {known}: drop the README's note and KNOWN_REFUSALS\n"
                f"{result.output}"
            )
        else:
            assert result.exit_code == 0, f"README line {line!r} exited {result.exit_code}:\n{result.output}"
        out[" ".join(argv[1:3])] = result.stdout
        ran += 1
    return ran


def test_the_readme_walk_runs_in_order_and_prints_what_it_shows(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # A stranger's clean install has git on PATH and not this suite's venv (no pytest, no tools it brought), so the
    # walk runs with PATH reduced to git alone: a command the README shows that needs anything else goes red here.
    git_bin = tmp_path / "path-git-only"
    git_bin.mkdir()
    (git_bin / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(git_bin))
    blocks = _blocks(_section())
    bash = [body for lang, body in blocks if lang == "bash"]
    printed = [body for lang, body in blocks if lang == ""]
    assert len(bash) >= 2 and printed, "fixture check: the section's setup, lane and output blocks were found"

    lane: dict[str, str] = {}
    ran = 0
    for index, body in enumerate(bash):
        if index == 2:
            # The README's "Edit there" step, between the lane block and the commit block: a commit that commits
            # nothing anywhere refuses, so the walk needs a change in a lane repo to stage.
            lane_repo = Path(lane["lane create"].splitlines()[1].split(": ", 1)[1])
            (lane_repo / "CHANGE").write_text("my change\n")
        ran += _run_block(body, ws, monkeypatch, lane)
        if index == 1:
            shown = [line.replace("~/ws", str(ws)) for line in printed[0].strip().splitlines()]
            assert lane["lane create"].strip().splitlines() == shown, (
                "the README's printed block must be what `lane create` prints"
            )
            for line in shown:
                path = Path(line.split(": ", 1)[-1])
                assert path.exists(), f"the README names a path that does not exist after `lane create`: {path}"

    # Every gr2 line in the section ran, whatever its fence says: a block re-fenced as ```sh would otherwise drop out.
    gr2_lines = sum(1 for _, body in blocks for line in body.splitlines() if line.strip().startswith("gr2 "))
    assert ran == gr2_lines, f"ran {ran} of the section's {gr2_lines} gr2 lines"
    # The walk's last line, `lane remove`, ends the lane: its record and its clones are gone.
    for line in shown:
        path = Path(line.split(": ", 1)[-1])
        assert not path.exists(), f"`lane remove` left a path the README says it removes: {path}"


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


def test_the_readme_review_install_block_parses_to_an_install_that_brings_pytest(tmp_path: Path) -> None:
    """The ".review-install" block in "Run a review's tests", read by the real hint parser and filled in by the real
    placeholder substitution, installs the lane AND pytest with the lane venv's Python. Without pytest the run
    refuses pytest_not_installed, which is what a stranger met following the one-line block."""
    from gr2.python_cli import review_run as rr

    text = README.read_text(encoding="utf-8")
    start = text.index("## Run a review's tests")
    section = text[start:text.index("\n## ", start + 1)]
    hint_blocks = [body for lang, body in _blocks(section) if lang == "" and ".review-install" in body]
    assert len(hint_blocks) == 1, "fixture check: exactly one .review-install block in the section"
    lane = tmp_path / "lane with space"
    lane.mkdir()
    (lane / ".review-install").write_text(hint_blocks[0])

    hint = rr.read_install_hint(lane)

    assert hint is not None and hint.get("package"), hint
    venv_python = tmp_path / "venv" / "bin" / "python"
    tokens = rr._apply_install_placeholders(shlex.split(hint["install"]), venv_python, lane)
    assert tokens == [str(venv_python), "-m", "pip", "install", "-e", str(lane), "pytest"], tokens


FULL_MODE = "## Full mode: one root, two desks"
# The README's "edit a file" steps, keyed by the commit line that follows each one: the member whose README the
# step edits in the desk the walk stands in. Keyed by the message, so moving or adding a block above does not shift it.
FULL_MODE_EDITS = {"api: first change": "api", "web: a change": "web", "api: a change from desk B": "api"}


def _named_section(heading: str) -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index(heading + "\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_readme_full_mode_walk_runs_across_two_desks(tmp_path: Path, monkeypatch) -> None:
    """The full-mode section, read from README.md by its heading and run in order: desk A makes the root and pushes,
    desk B clones it and checks out, and each desk pulls the other's change. The hand step the section asks for (the
    root's remote) turns this red at gr2 push when it is deleted."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = You\n\temail = you@example.com\n")
    monkeypatch.setenv("HOME", str(home))
    urls = {}
    for name in ("api", "web"):
        remote = tmp_path / f"{name}.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
        seed = tmp_path / f"seed-{name}"
        _git(tmp_path, "clone", "-q", str(remote), str(seed))
        (seed / "README").write_text(f"{name}\n")
        _git(seed, "add", "README")
        _git(seed, "commit", "-qm", "init")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        urls[name] = str(remote)
    root_remote = tmp_path / "root.git"
    _git(tmp_path, "init", "-q", "--bare", str(root_remote))
    desk_a, desk_b = tmp_path / "full", tmp_path / "full-b"
    words = {"$API": urls["api"], "$WEB": urls["web"], "$ROOT": str(root_remote),
             "~/full-b": str(desk_b), "~/full": str(desk_a)}
    git_bin = tmp_path / "path-git-only"
    git_bin.mkdir()
    (git_bin / "git").symlink_to(shutil.which("git"))
    git_exe = shutil.which("git")
    monkeypatch.setenv("PATH", str(git_bin))
    monkeypatch.chdir(tmp_path)

    blocks = [body for lang, body in _blocks(_named_section(FULL_MODE)) if lang == "bash"]
    assert len(blocks) >= 5, "fixture check: the section's command blocks were found"
    ran, edited = 0, []
    for body in blocks:
        for line in body.splitlines():
            line = line.split("  #", 1)[0].strip()
            if not line:
                continue
            for word, value in words.items():
                line = line.replace(word, value)
            argv = shlex.split(line)
            if argv[0] == "cd":
                monkeypatch.chdir(argv[1])
                continue
            if argv[0] == "git":
                result = subprocess.run([git_exe, *argv[1:]], capture_output=True, text=True)
                assert result.returncode == 0, f"README line {line!r} exited {result.returncode}:\n{result.stderr}"
                continue
            assert argv[0] == "gr2", f"README full-mode line is neither gr2, git nor cd: {line!r}"
            if argv[1:3] == ["commit", "-a"]:
                message = argv[argv.index("-m") + 1]
                member = Path.cwd() / FULL_MODE_EDITS[message]
                with (member / "README").open("a") as handle:
                    handle.write(f"{message}\n")
                edited.append(message)
            result = runner.invoke(app, argv[1:], catch_exceptions=False)
            assert result.exit_code == 0, f"README line {line!r} exited {result.exit_code}:\n{result.output}"
            ran += 1

    assert sorted(edited) == sorted(FULL_MODE_EDITS), f"fixture check: every edit step ran once, ran {edited}"
    gr2_lines = sum(1 for body in blocks for line in body.splitlines() if line.strip().startswith("gr2 "))
    assert ran == gr2_lines, f"ran {ran} of the section's {gr2_lines} gr2 lines"
    # Both desks end holding every change: desk B's web carries desk A's, and desk A's api carries desk B's.
    for desk in (desk_a, desk_b):
        api = (desk / "api" / "README").read_text()
        web = (desk / "web" / "README").read_text()
        assert "api: first change\n" in api and "api: a change from desk B\n" in api, (desk, api)
        assert "web: a change\n" in web, (desk, web)
