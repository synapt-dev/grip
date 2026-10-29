"""`store init` on a root that is not a directory: compat mode adopts the checkouts already beside it, so it refuses by
name (exit 2) and creates nothing, where it used to fail with a raw FileNotFoundError. Full mode still creates the
root, and --clone still makes it before cloning into it.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, gr2, make_member, run


def _refused_by_name(result, root: Path) -> None:
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert f"{str(root)!r} is not a directory" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr


def test_a_missing_root_is_refused_by_name_and_nothing_is_created(tmp_path):
    root = tmp_path / "missing"

    result = gr2(tmp_path, "init", str(root), check=False)

    _refused_by_name(result, root)
    assert not root.exists()


def test_a_root_with_a_missing_parent_is_refused_the_same(tmp_path):
    root = tmp_path / "nope" / "missing"

    result = gr2(tmp_path, "init", str(root), check=False)

    _refused_by_name(result, root)
    assert not (tmp_path / "nope").exists()


def test_an_existing_empty_root_is_still_the_no_members_refusal(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    result = gr2(tmp_path, "init", str(root), check=False)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "no sibling git repositories" in result.stderr


def test_a_root_with_two_member_checkouts_still_inits(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)

    result = gr2(tmp_path, "init", str(root), check=False)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (root / "grip.toml").exists()


def test_a_root_that_is_a_file_is_refused_by_name(tmp_path):
    root = tmp_path / "afile"
    root.write_text("x\n", encoding="utf-8")

    result = gr2(tmp_path, "init", str(root), check=False)

    _refused_by_name(result, root)
    assert root.read_text(encoding="utf-8") == "x\n"


def test_full_mode_still_creates_a_missing_root(tmp_path):
    root = tmp_path / "missing"

    result = gr2(tmp_path, "init", str(root), "--mode", "full", check=False)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert root.is_dir()


def test_clone_into_a_missing_root_still_inits(tmp_path):
    root = tmp_path / "missing"
    gamma, _ = make_member(tmp_path, "gamma")

    result = gr2(tmp_path, "init", str(root), "--clone", f"gamma={gamma}", check=False)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (root / "gamma" / ".git").exists()
