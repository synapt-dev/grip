"""Rows for a gitlink in the root index that grip.toml does not declare.

`git add` at the root stages a nested repository as a gitlink (mode 160000); the generated
.gitignore hides one from a plain `git add -A`, but `git add -f`, or a root without that
.gitignore, stages it. `store check`
walked only the declared members, so it printed "Checked 2 member(s)" and exited 0 while the
root index carried a pin nobody declared, and the next `store commit` would publish it. Check
now exits 3, names each undeclared gitlink path, and names the command that removes it from
the index. The check is local, so it refuses before any upstream is asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_store_break_attempts import (  # noqa: E402
    _assert_init_ran,
    _cli,
    _git,
    _git_out,
    two_member_ws,
)


def _committed(root: Path) -> None:
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "first record")[0] == 0


def _embedded_repo(root: Path, rel: str) -> None:
    """A nested repository with one commit, force-staged past the generated .gitignore."""
    path = root / rel
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "f.txt").write_text(rel)
    _git(path, "add", "f.txt")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "nested")
    _git(root, "add", "-f", "--", rel)
    assert _git_out(root, "ls-files", "-s", "--", rel).startswith("160000 "), f"{rel} must be a staged gitlink"


def _respell_member_path(root: Path, name: str, spelling: str) -> None:
    """Rewrite one member's `path` in grip.toml to another spelling of the same directory."""
    toml = root / "grip.toml"
    before = toml.read_text()
    after = before.replace(f'path = "{name}"\n', f'path = "{spelling}"\n', 1)
    assert after != before, f"grip.toml must carry path = {name!r}"
    toml.write_text(after)


def test_check_passes_with_only_declared_members(two_member_ws: Path) -> None:
    """Control: the declared members are gitlinks too, and none of them is reported."""
    root = two_member_ws
    _committed(root)

    rc, out = _cli("store", "check")

    assert rc == 0, out
    assert "Checked 2 member(s)" in out
    assert "undeclared" not in out


def test_check_refuses_one_undeclared_gitlink(two_member_ws: Path) -> None:
    root = two_member_ws
    _committed(root)
    _embedded_repo(root, "stray")

    rc, out = _cli("store", "check")

    assert rc == 3, out
    assert "undeclared gitlink" in out
    assert "stray" in out
    assert "git rm --cached -f -- stray" in out
    assert "Checked" not in out
    # The advice is true: following it clears the refusal and leaves the nested repo on disk.
    _git(root, "rm", "-q", "--cached", "-f", "--", "stray")
    rc, out = _cli("store", "check")
    assert rc == 0, out
    assert (root / "stray" / "f.txt").read_text() == "stray"


def test_check_names_every_undeclared_gitlink(two_member_ws: Path) -> None:
    root = two_member_ws
    _committed(root)
    _embedded_repo(root, "stray-one")
    _embedded_repo(root, "stray-two")

    rc, out = _cli("store", "check")

    assert rc == 3, out
    assert "git rm --cached -f -- stray-one" in out
    assert "git rm --cached -f -- stray-two" in out


def test_check_refuses_an_undeclared_gitlink_in_a_subdirectory(two_member_ws: Path) -> None:
    root = two_member_ws
    _committed(root)
    _embedded_repo(root, "vendor/nested")

    rc, out = _cli("store", "check")

    assert rc == 3, out
    assert "git rm --cached -f -- vendor/nested" in out


@pytest.mark.parametrize("spelling", ["./alpha", "alpha/"])
def test_check_never_reports_a_declared_member_spelled_another_way(two_member_ws: Path, spelling: str) -> None:
    """grip.toml may spell a member's path `./alpha` or `alpha/`; the index spells it `alpha`.
    The declared set is compared through PurePosixPath, as the rest of the CLI compares member paths."""
    root = two_member_ws
    _committed(root)
    _respell_member_path(root, "alpha", spelling)
    assert _git_out(root, "ls-files", "-s", "--", "alpha").startswith("160000 "), "alpha must be a staged gitlink"

    rc, out = _cli("store", "check")

    assert rc == 0, out
    assert "undeclared" not in out
