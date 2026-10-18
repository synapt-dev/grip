"""`store init --mode`: the one way into full mode that is not a hand edit of grip.toml.

A new store is written in the mode asked for; a root whose grip.toml already declares another mode is refused before
anything is cloned or written, because init does not switch a workspace's mode.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import workspace_mode
from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _two_members(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    return root


def test_mode_full_writes_the_line_and_the_first_commit_records_field_commits(tmp_path):
    root = _two_members(tmp_path)

    assert gr2(root, "init", "--mode", "full").returncode == 0
    configure_identity(root)

    assert workspace_mode.of_root(root) == "full"
    first = gr2(root, "commit", "-m", "first")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    # In full mode the gitlink is a field-commit carrying the record, not the member's own commit.
    gitlink = git(root, "ls-tree", "HEAD", "--", "alpha").stdout.split()[2]
    assert gitlink != git(root / "alpha", "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("args", [(), ("--mode", "compat")], ids=["no-mode", "mode-compat"])
def test_no_mode_and_mode_compat_write_no_mode_line(tmp_path, args):
    root = _two_members(tmp_path)

    assert gr2(root, "init", *args).returncode == 0

    assert workspace_mode.of_root(root) == "compat"
    assert "mode = \"full\"\n" not in (root / "grip.toml").read_text(encoding="utf-8").split("[[members]]")[0]


def test_an_unknown_mode_is_a_usage_error_that_creates_nothing(tmp_path):
    root = _two_members(tmp_path)

    result = gr2(root, "init", "--mode", "fast", check=False)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "'fast'" in result.stderr
    assert not (root / ".git").exists() and not (root / "grip.toml").exists()


@pytest.mark.parametrize("first,second", [("compat", "full"), ("full", "compat")])
def test_init_does_not_switch_an_existing_stores_mode(tmp_path, first, second):
    root = _two_members(tmp_path)
    assert gr2(root, "init", "--mode", first).returncode == 0
    before = (root / "grip.toml").read_bytes()

    result = gr2(root, "init", "--mode", second, check=False)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert f"declares mode {first!r}" in result.stderr and f"--mode {second}" in result.stderr
    assert (root / "grip.toml").read_bytes() == before


def test_reinit_with_the_same_mode_is_idempotent(tmp_path):
    root = _two_members(tmp_path)
    assert gr2(root, "init", "--mode", "full").returncode == 0
    before = (root / "grip.toml").read_bytes()

    assert gr2(root, "init", "--mode", "full").returncode == 0

    assert (root / "grip.toml").read_bytes() == before


@pytest.mark.parametrize("args,refused", [((), False), (("--mode", "compat"), True)], ids=["control", "mode-compat"])
def test_the_mode_refusal_comes_before_any_clone(tmp_path, args, refused):
    """A root holding only a grip.toml that declares full mode: with no --mode, --clone clones (the control, so the
    refusal row cannot pass because cloning was refused for some other reason); with --mode compat, nothing is cloned."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "grip.toml").write_text('mode = "full"\n', encoding="utf-8")
    gamma, _ = make_member(tmp_path, "gamma")

    result = gr2(root, "init", *args, "--clone", f"gamma={gamma}", check=False)

    assert result.returncode == (4 if refused else 0), (result.stdout, result.stderr)
    assert (root / "gamma").exists() is not refused
