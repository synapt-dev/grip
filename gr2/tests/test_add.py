"""Executable contract for the native single-repository ``gr2 add`` verb."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "keep.txt").write_text("before\n")
    (path / "delete.txt").write_text("delete me\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def _cached_names(repo: Path) -> list[str]:
    output = _git(repo, "diff", "--cached", "--name-only", "-z").stdout
    return [name for name in output.split("\0") if name]


def test_stage_files_stages_added_modified_and_deleted_paths(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_text("after\n")
    (repo / "delete.txt").unlink()
    (repo / "new.txt").write_text("new\n")

    result = stage_files(repo, ["keep.txt", "delete.txt", "new.txt"])

    assert result.staged_files == ("delete.txt", "keep.txt", "new.txt")
    assert _cached_names(repo) == ["delete.txt", "keep.txt", "new.txt"]


def test_deleted_path_is_staged_instead_of_misclassified_as_missing(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "delete.txt").unlink()

    result = stage_files(repo, ["delete.txt"])

    assert result.staged_files == ("delete.txt",)
    assert (
        _git(repo, "diff", "--cached", "--diff-filter=D", "--name-only").stdout.strip()
        == "delete.txt"
    )


def test_never_existing_path_refuses_without_touching_the_index(tmp_path: Path) -> None:
    from gr2.python_cli.add import AddError, stage_files

    repo = _init_repo(tmp_path / "repo")

    with pytest.raises(AddError):
        stage_files(repo, ["never-existed.txt"])

    assert _cached_names(repo) == []


def test_result_is_scoped_to_requested_paths_not_the_whole_index(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_text("already staged\n")
    _git(repo, "add", "keep.txt")
    (repo / "new.txt").write_text("requested\n")

    result = stage_files(repo, ["new.txt"])

    assert result.staged_files == ("new.txt",)
    assert _cached_names(repo) == ["keep.txt", "new.txt"]


def test_add_cli_defaults_to_the_cwd_repository_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    repo_a = _init_repo(tmp_path / "a")
    repo_b = _init_repo(tmp_path / "b")
    (repo_a / "new.txt").write_text("a\n")
    (repo_b / "new.txt").write_text("b\n")
    monkeypatch.chdir(repo_a)

    result = CliRunner().invoke(app, ["add", "new.txt"])

    assert result.exit_code == 0, result.output
    assert _cached_names(repo_a) == ["new.txt"]
    assert _cached_names(repo_b) == []


def test_add_cli_honors_explicit_repo_path_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    cwd_repo = _init_repo(tmp_path / "cwd")
    requested_repo = _init_repo(tmp_path / "requested")
    (cwd_repo / "new.txt").write_text("cwd\n")
    (requested_repo / "new.txt").write_text("requested\n")
    monkeypatch.chdir(cwd_repo)

    result = CliRunner().invoke(
        app,
        ["add", "--repo-path", str(requested_repo), "new.txt"],
    )

    assert result.exit_code == 0, result.output
    assert _cached_names(cwd_repo) == []
    assert _cached_names(requested_repo) == ["new.txt"]


# --- untracked Python bytecode is skipped by default -------------------------------------------------------------------
# Each repo here ignores NOTHING (no .gitignore), which is what a test fixture and a first-time user's repo look like.


def _bytecode_repo(path: Path) -> Path:
    repo = _init_repo(path)
    (repo / "src" / "__pycache__").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("VALUE = 1\n")
    (repo / "src" / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\x00bytecode")
    (repo / "x.pyc").write_bytes(b"\x00loose")  # outside any __pycache__ directory (Apollo's ask 3)
    (repo / "y.pyo").write_bytes(b"\x00optimized")
    return repo


def test_add_dot_skips_untracked_bytecode_and_names_it_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli.app import app

    repo = _bytecode_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["add", "."])
    assert result.exit_code == 0, result.output
    assert _cached_names(repo) == ["src/m.py"]
    skips = [line for line in result.output.splitlines() if line.startswith("Skipped ")]
    assert skips == ["Skipped 3 untracked bytecode path(s) (__pycache__/, *.pyc, *.pyo), e.g. src/__pycache__/m.cpython-313.pyc; "
                     "pass --include-bytecode to stage them"], result.output


def test_add_dot_from_a_subdirectory_skips_its_bytecode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cd src && gr2 add .`: git refuses `rm --pathspec-from-file` from a subdirectory (it adds the cwd as a pathspec
    of its own), so the unstage runs from the top level, on top-level names, and the bytecode is never left staged."""
    from gr2.python_cli.app import app

    repo = _bytecode_repo(tmp_path / "repo")
    (repo / "src" / "new.pyc").write_bytes(b"\x00loose in a subdirectory")
    monkeypatch.chdir(repo / "src")
    result = CliRunner().invoke(app, ["add", "."])
    assert result.exit_code == 0, result.output
    assert _cached_names(repo) == ["src/m.py"]
    skips = [line for line in result.output.splitlines() if line.startswith("Skipped ")]
    assert skips == ["Skipped 2 untracked bytecode path(s) (__pycache__/, *.pyc, *.pyo), e.g. src/__pycache__/m.cpython-313.pyc; "
                     "pass --include-bytecode to stage them"], result.output


def test_pathspecs_that_leave_the_subdirectory_skip_bytecode_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli.app import app

    repo = _bytecode_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")
    result = CliRunner().invoke(app, ["add", "../x.pyc", "../y.pyo", "m.py"])
    assert result.exit_code == 0, result.output
    assert _cached_names(repo) == ["src/m.py"]
    skips = [line for line in result.output.splitlines() if line.startswith("Skipped ")]
    assert skips == ["Skipped 2 untracked bytecode path(s) (__pycache__/, *.pyc, *.pyo), e.g. x.pyc; "
                     "pass --include-bytecode to stage them"], result.output


def test_include_bytecode_stages_it_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli.app import app

    repo = _bytecode_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["add", "--include-bytecode", "."])
    assert result.exit_code == 0, result.output
    assert _cached_names(repo) == ["src/__pycache__/m.cpython-313.pyc", "src/m.py", "x.pyc", "y.pyo"]
    assert "Skipped" not in result.output


def test_tracked_bytecode_stages_a_change_and_a_delete_as_git_does(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\x00one")
    (repo / "old.pyc").write_bytes(b"\x00two")
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-m", "bytecode committed by mistake")
    (repo / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\x00changed")  # Apollo's ask 2: a change
    (repo / "old.pyc").unlink()  # the cleanup case: a delete

    result = stage_files(repo, ["."])

    assert result.skipped_bytecode == ()
    assert _cached_names(repo) == ["old.pyc", "pkg/__pycache__/a.pyc"]
    assert _git(repo, "diff", "--cached", "--diff-filter=D", "--name-only").stdout.split() == ["old.pyc"]


def test_a_repo_with_no_bytecode_stages_as_before_and_prints_no_skip_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    repo = _init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_text("after\n")
    (repo / "new.txt").write_text("new\n")
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["add", "."])
    assert result.exit_code == 0, result.output
    assert _cached_names(repo) == ["keep.txt", "new.txt"]
    assert "Skipped" not in result.output


def test_a_gitignore_that_already_covers_bytecode_leaves_nothing_to_skip(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _bytecode_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("__pycache__/\n*.py[co]\n")

    result = stage_files(repo, ["."])

    assert result.skipped_bytecode == ()
    assert _cached_names(repo) == [".gitignore", "src/m.py"]


def test_names_that_only_look_like_bytecode_still_stage(tmp_path: Path) -> None:
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "pycache_notes.txt").write_text("notes\n")
    (repo / "my__pycache__.md").write_text("doc\n")
    (repo / "x.pyc.txt").write_text("not bytecode\n")
    (repo / "glob[1].pyc").write_bytes(b"\x00bracket name")  # a glob character in a skipped name stays literal

    result = stage_files(repo, ["."])

    assert result.skipped_bytecode == ("glob[1].pyc",)
    assert _cached_names(repo) == ["my__pycache__.md", "pycache_notes.txt", "x.pyc.txt"]


def test_a_skipped_name_with_glob_characters_never_unstages_a_tracked_file_it_would_match(tmp_path: Path) -> None:
    """The unstage takes each skipped name LITERALLY: as a glob, `[ab].pyc` would also match the tracked `a.pyc` and take
    a committed file out of the index."""
    from gr2.python_cli.add import stage_files

    repo = _init_repo(tmp_path / "repo")
    (repo / "a.pyc").write_bytes(b"\x00tracked")
    _git(repo, "add", "-f", "a.pyc")
    _git(repo, "commit", "-m", "tracked bytecode")
    (repo / "a.pyc").write_bytes(b"\x00tracked, changed")
    (repo / "[ab].pyc").write_bytes(b"\x00untracked")

    result = stage_files(repo, ["."])

    assert result.skipped_bytecode == ("[ab].pyc",)
    assert _cached_names(repo) == ["a.pyc"]
    assert _git(repo, "diff", "--cached", "--diff-filter=M", "--name-only").stdout.split() == ["a.pyc"]
