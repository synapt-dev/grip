"""`store commit` refuses a staged change to a path the store does not write.

Without a `.gitinclude`, nothing judged the root's index: a file the author had staged by hand rode into the record
commit beside the pins. The commit now judges the staged diff against HEAD and refuses any path in it that the
commit does not write itself. A file the root already tracks and has not changed is not in that diff, so an adopted
root's own files are left alone.

Premium boundary: OSS (grip). Local workspace record over git; no identity, org, or entitlement semantics.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _store(tmp_path: Path, adopted: bool = False) -> Path:
    """One member and a committed root, built as test_store_commit_keeps_record's `_root_with_declarations` builds
    one; `adopted` first makes the root a repository with its own tracked file."""
    root = tmp_path / "workspace"
    root.mkdir()
    if adopted:
        git(root, "init", "-q")
        configure_identity(root)
        (root / "notes.md").write_text("the root's own file\n")
        git(root, "add", "notes.md")
        git(root, "commit", "-q", "-m", "root")
    remote, _ = make_member(tmp_path, "alpha")
    run(root, "git", "clone", str(remote), "alpha")
    configure_identity(root / "alpha")
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0
    if (root / ".gitinclude").exists():   # init declares one on a fresh root; these rows judge a root without
        git(root, "rm", "-q", ".gitinclude")
        git(root, "commit", "-q", "-m", "drop the declaration")
    assert not (root / ".gitinclude").exists(), "precondition: no declaration"
    return root


def _move_alpha(root: Path) -> None:
    (root / "alpha" / "change.txt").write_text("x\n")
    git(root / "alpha", "add", "change.txt")
    git(root / "alpha", "commit", "-q", "-m", "change")
    git(root / "alpha", "push", "-q", "origin", "HEAD:main")


def _index(root: Path) -> str:
    return git(root, "ls-files", "-s").stdout


def test_a_staged_stray_file_is_refused_and_left_staged(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _move_alpha(root)
    (root / "stray.txt").write_text("not the store's\n")
    git(root, "add", "-f", "stray.txt")
    before, index = git(root, "rev-parse", "HEAD").stdout, _index(root)

    done = gr2(root, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 4, f"{done.stdout}\n{done.stderr}"
    assert "stray.txt" in done.stdout + done.stderr
    assert git(root, "rev-parse", "HEAD").stdout == before, "the refusal makes no commit"
    assert _index(root) == index, "the author's index is not cleared"
    assert "stray.txt" in git(root, "diff", "--cached", "--name-only").stdout, "the stray is still staged"


def test_a_clean_pin_change_still_commits(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _move_alpha(root)
    before = git(root, "rev-parse", "HEAD").stdout

    done = gr2(root, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(root, "rev-parse", "HEAD").stdout != before


def test_an_adopted_roots_unchanged_file_does_not_block_a_commit(tmp_path: Path) -> None:
    root = _store(tmp_path, adopted=True)
    _move_alpha(root)
    before = git(root, "rev-parse", "HEAD").stdout

    done = gr2(root, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(root, "rev-parse", "HEAD").stdout != before
    assert "notes.md" in git(root, "ls-tree", "--name-only", "HEAD").stdout, "the root's own file is kept"


def test_an_adopted_roots_staged_edit_is_refused(tmp_path: Path) -> None:
    root = _store(tmp_path, adopted=True)
    _move_alpha(root)
    (root / "notes.md").write_text("edited\n")
    git(root, "add", "notes.md")
    before, index = git(root, "rev-parse", "HEAD").stdout, _index(root)

    done = gr2(root, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 4, f"{done.stdout}\n{done.stderr}"
    assert "notes.md" in done.stdout + done.stderr
    assert git(root, "rev-parse", "HEAD").stdout == before
    assert _index(root) == index


def test_an_adopted_roots_file_renamed_into_the_fields_dir_is_refused(tmp_path: Path) -> None:
    root = _store(tmp_path, adopted=True)
    _move_alpha(root)
    (root / ".grip" / "fields").mkdir(parents=True, exist_ok=True)
    git(root, "mv", "notes.md", ".grip/fields/notes.md")
    before, index = git(root, "rev-parse", "HEAD").stdout, _index(root)

    done = gr2(root, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 4, f"{done.stdout}\n{done.stderr}"
    assert "notes.md is staged" in done.stdout + done.stderr, "the source path, whose deletion is staged, is named"
    assert git(root, "rev-parse", "HEAD").stdout == before
    assert _index(root) == index
    assert "notes.md" in git(root, "ls-tree", "--name-only", "HEAD").stdout.split(), "the root's file is still recorded"
