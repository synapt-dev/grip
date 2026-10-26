"""A review bind from a root that is not yet a store names what stops it, one state at a time.

A bind on a workspace root with no store runs `store init` first (see test_review_bind_native_store.py).
When `store init` cannot, the reader gets its reason, and these rows pin that the reason is the right
one and carries one fix:
  * a declared member with nothing at its path is ABSENT, not "a folder";
  * a declared member that is a plain directory is a folder, not a git checkout;
  * the refusal says, before it happens, that the first bind there makes the root a native store;
  * an empty directory gets ONE remedy, and is told that `store init` needs a member checkout there;
  * a directory that does hold a member checkout keeps the plain remedy.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


def _bind(root: Path, ws: Path) -> tuple[int, str]:
    alpha = ws / "alpha"
    remote = _git_out(alpha, "remote", "get-url", "origin")
    base = _git_out(alpha, "rev-parse", "origin/main")
    (alpha / "pin-row.txt").write_text("under review\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "the change under review")
    head = _git_out(alpha, "rev-parse", "HEAD")
    return _cli("review", "bind", str(root), "--repo", "alpha", "--remote", remote, "--base", base,
                "--head", head, "--ref", "refs/heads/main", "--source", str(alpha))


def _spec_root(tmp_path: Path, member_path: str) -> Path:
    root = tmp_path / "desk"
    (root / ".grip").mkdir(parents=True)
    (root / ".grip" / "workspace_spec.toml").write_text(
        f'workspace_name = "desk"\n\n[[repos]]\nname = "m"\npath = "{member_path}"\n'
        f'url = "https://example.invalid/m.git"\n'
    )
    return root


def test_a_declared_member_absent_on_disk_is_named_absent(tmp_path: Path, two_member_ws: Path) -> None:
    root = _spec_root(tmp_path, "ghost")
    code, out = _bind(root, two_member_ws)
    assert code == 2 and "store_init_refused" in out, out
    assert f"declared member 'ghost' is ABSENT: nothing at {root.resolve() / 'ghost'}" in out or \
        f"declared member 'ghost' is ABSENT: nothing at {root / 'ghost'}" in out, out
    assert "clone it there, or drop it from the member list" in out, out
    assert "is a folder" not in out and "not a folder" not in out, out
    assert not (root / ".git").exists(), "a refused bind left a root .git behind"


def test_a_declared_member_that_is_a_plain_folder_is_named_a_folder(tmp_path: Path, two_member_ws: Path) -> None:
    root = _spec_root(tmp_path, "reference/git-repo")
    (root / "reference" / "git-repo").mkdir(parents=True)
    code, out = _bind(root, two_member_ws)
    assert code == 2 and "store_init_refused" in out, out
    assert "member path 'reference/git-repo' is a folder, not a git checkout" in out, out
    assert "make it a clone, or drop it from the member list" in out, out
    assert "ABSENT" not in out, out


def test_the_refusal_names_that_a_first_bind_makes_the_root_a_store(tmp_path: Path, two_member_ws: Path) -> None:
    root = _spec_root(tmp_path, "ghost")
    code, out = _bind(root, two_member_ws)
    assert code == 2, out
    assert "the first pin here runs `store init` and makes" in out and "a native store" in out, out


def test_an_empty_dir_gets_one_remedy_and_is_told_store_init_needs_a_member(tmp_path: Path,
                                                                          two_member_ws: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = _bind(empty, two_member_ws)
    assert code == 2 and "not_initialized" in out, out
    assert out.count("Run `gr2 store init .` first.") == 1, out
    assert "`store init` needs at least one member checkout in this directory, and it has none." in out, out
    assert "grip init" not in out, out


def test_a_dir_holding_a_member_checkout_keeps_the_plain_remedy(tmp_path: Path, two_member_ws: Path) -> None:
    near = tmp_path / "near"
    (near / "child").mkdir(parents=True)
    _git(near / "child", "init", "-q", "-b", "main")
    code, out = _bind(near, two_member_ws)
    assert code == 2 and "not_initialized" in out, out
    assert "Run `gr2 store init .` first." in out, out
    assert "needs at least one member checkout" not in out, out
