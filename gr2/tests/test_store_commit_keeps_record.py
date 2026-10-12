"""`store commit` keeps what the record declares and it does not own.

Measured on a fresh clone of a committed root, in a three-operator review cell on grip dev fff9faf: a `store commit`
rewrote `workspace_name` to the clone's directory name, dropped `.gitinclude`, and dropped the `[approvals]` table,
so from the second feature on the review gate read `required = 0` and merged with rc 0. The writer derives only
the members; everything else in grip.toml is the owner's declaration and is carried through.

Premium boundary: OSS (grip). Local workspace record over git; no identity, org, or entitlement semantics.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _root_with_declarations(tmp_path: Path) -> tuple[Path, Path]:
    """Two members, a committed root carrying `[approvals]` and an unknown owner table, pushed; and a fresh
    no-checkout clone of it in a directory whose name differs from the root's."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    spec = (root / "grip.toml").read_text()
    (root / "grip.toml").write_text(spec.rstrip("\n") + '\n\n[approvals]\nrequired = 2\n\n[owner_notes]\nteam = "core"\n')
    assert gr2(root, "commit", "-m", "first").returncode == 0
    committed = tomllib.loads(git(root, "show", "HEAD:grip.toml").stdout)
    assert committed["approvals"] == {"required": 2}, "precondition: the first commit records the owner's tables"
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", "--no-checkout", str(root_remote), str(fresh))
    return root, fresh


def _materialized(tmp_path: Path) -> tuple[Path, Path]:
    root, fresh = _root_with_declarations(tmp_path)
    assert gr2(fresh, "materialize").returncode == 0
    configure_identity(fresh)
    for name in ("alpha", "beta"):
        configure_identity(fresh / name)
    return root, fresh


def test_a_no_op_commit_on_a_fresh_clone_changes_nothing(tmp_path: Path) -> None:
    _, fresh = _materialized(tmp_path)
    before = git(fresh, "rev-parse", "HEAD").stdout.strip()

    done = gr2(fresh, "commit", "-m", "nothing", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh, "rev-parse", "HEAD").stdout.strip() == before, git(fresh, "show", "--stat", "HEAD").stdout


def test_a_real_commit_keeps_workspace_name_approvals_owner_tables_and_gitinclude(tmp_path: Path) -> None:
    _, fresh = _materialized(tmp_path)
    (fresh / "alpha" / "change.txt").write_text("x\n")
    git(fresh / "alpha", "add", "change.txt")
    git(fresh / "alpha", "commit", "-q", "-m", "change")
    git(fresh / "alpha", "push", "-q", "origin", "HEAD:main")   # a full-mode pin must be on its remote first

    done = gr2(fresh, "commit", "-m", "alpha moved", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    committed = tomllib.loads(git(fresh, "show", "HEAD:grip.toml").stdout)
    assert committed["workspace_name"] == "workspace", "the record's name, not the clone's directory name"
    assert committed["approvals"] == {"required": 2}, "the review gate's floor survives the commit"
    assert committed["owner_notes"] == {"team": "core"}, "a table the writer does not own is carried through"
    alpha = next(m for m in committed["members"] if m["name"] == "alpha")
    assert alpha["pin"] == git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip(), "the member the writer owns moved"
    tracked = git(fresh, "ls-tree", "--name-only", "HEAD").stdout.split()
    assert ".gitinclude" in tracked, git(fresh, "show", "--stat", "HEAD").stdout


def test_a_first_commit_in_a_new_workspace_still_names_it_after_its_directory(tmp_path: Path) -> None:
    """Control: with no record yet, the writer derives the name and seeds `.gitinclude`, as before."""
    root = tmp_path / "brandnew"
    root.mkdir()
    remote, _ = make_member(tmp_path, "alpha")
    run(root, "git", "clone", str(remote), "alpha")
    configure_identity(root / "alpha")
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0
    committed = tomllib.loads(git(root, "show", "HEAD:grip.toml").stdout)
    assert committed["workspace_name"] == "brandnew"
    assert "approvals" not in committed
    assert ".gitinclude" in git(root, "ls-tree", "--name-only", "HEAD").stdout.split()


def test_materialize_never_overwrites_a_root_that_already_holds_a_checkout(tmp_path: Path) -> None:
    """The restore fills only an EMPTY index: a normal clone with a local grip.toml edit keeps the edit."""
    _, _ = _root_with_declarations(tmp_path)
    clone = tmp_path / "normal"
    run(tmp_path, "git", "clone", "-q", str(tmp_path / "root.git"), str(clone))
    edited = (clone / "grip.toml").read_text() + '\n[local_edit]\nkept = true\n'
    (clone / "grip.toml").write_text(edited)
    git(clone, "add", "grip.toml")   # staged work: the index is not empty, so the restore must not run

    assert gr2(clone, "materialize").returncode == 0

    assert (clone / "grip.toml").read_text() == edited, "materialize rewrote a root file the operator had edited"
    assert "[local_edit]" in git(clone, "diff", "--cached", "grip.toml").stdout, "materialize unstaged the operator's work"


def test_a_no_checkout_clone_gets_the_roots_own_files_back(tmp_path: Path) -> None:
    _, fresh = _root_with_declarations(tmp_path)
    assert not (fresh / "grip.toml").exists(), "precondition: a --no-checkout clone has no root files"

    assert gr2(fresh, "materialize").returncode == 0

    for name in ("grip.toml", ".gitinclude", ".gitmodules"):
        assert (fresh / name).read_bytes() == git(fresh, "show", f"HEAD:{name}").stdout.encode(), name
    assert git(fresh, "status", "--porcelain").stdout == "", git(fresh, "status", "--short").stdout


def test_materialize_leaves_an_index_the_operator_emptied_alone(tmp_path: Path) -> None:
    """An emptied index is still a file, and is staged work: `git rm -r --cached .` in a normal clone. Only a root with
    NO index file (a `--no-checkout` clone) is restored. Found in review: the first guard read "empty" as "absent"."""
    _, _ = _root_with_declarations(tmp_path)
    clone = tmp_path / "normal"
    run(tmp_path, "git", "clone", "-q", str(tmp_path / "root.git"), str(clone))
    git(clone, "rm", "-r", "-q", "--cached", ".")
    assert git(clone, "ls-files").stdout == "", "precondition: the operator's index is empty"
    staged = git(clone, "diff", "--cached", "--name-status").stdout

    done = gr2(clone, "materialize", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(clone, "ls-files").stdout == "", "materialize refilled an index the operator had emptied"
    assert git(clone, "diff", "--cached", "--name-status").stdout == staged, "the staged removals changed"


def test_a_no_checkout_root_with_a_stray_file_gets_the_others_and_keeps_the_stray(tmp_path: Path) -> None:
    """A file already on disk is never replaced and never fails the verb; the absent ones are still written."""
    _, fresh = _root_with_declarations(tmp_path)
    (fresh / ".gitinclude").write_text("stray\n")

    done = gr2(fresh, "materialize", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert (fresh / ".gitinclude").read_text() == "stray\n", "a file already on disk was replaced"
    assert (fresh / "grip.toml").read_bytes() == git(fresh, "show", "HEAD:grip.toml").stdout.encode()
