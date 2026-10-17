"""Rows for an undeclared gitlink in the root index, as `store status` and `store push` see it.

`store check` refuses a root index holding a gitlink at a path grip.toml does not declare.
`store status` printed the member table and exited 0 on the same root, so a caller that reads
only the exit code took it as healthy. Status now prints the table, then the same refusal check
prints, and exits 3. `store push` runs check before it publishes, so it refuses through the
same helper; its row pins that a committed undeclared gitlink is never pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_store_break_attempts import (  # noqa: E402
    _cli,
    _git,
    _git_out,
    _ls_remote,
    _set_root_origin,
    two_member_ws,
)
from tests.test_store_check_undeclared_gitlink import (  # noqa: E402
    _committed,
    _embedded_repo,
    _respell_member_path,
)


def test_status_passes_with_only_declared_members(two_member_ws: Path) -> None:
    """Control: the declared members are gitlinks too, and none of them is reported."""
    root = two_member_ws
    _committed(root)

    rc, out = _cli("store", "status")

    assert rc == 0, out
    assert "undeclared" not in out


def test_status_refuses_an_undeclared_gitlink_and_still_prints_the_table(two_member_ws: Path) -> None:
    root = two_member_ws
    _committed(root)
    _embedded_repo(root, "stray")

    rc, out = _cli("store", "status")

    assert rc == 3, out
    assert "undeclared gitlink 'stray' in the root index" in out
    assert "git rm --cached -f -- stray" in out
    # The table prints in every case; the code follows it.
    assert "alpha " in out and "beta " in out, out
    # The advice is true: following it clears the refusal and leaves the nested repo on disk.
    _git(root, "rm", "-q", "--cached", "-f", "--", "stray")
    rc, out = _cli("store", "status")
    assert rc == 0, out
    assert (root / "stray" / "f.txt").read_text() == "stray"


def test_push_refuses_a_committed_undeclared_gitlink(two_member_ws: Path, tmp_path: Path) -> None:
    root = two_member_ws
    root_remote = tmp_path / "root.git"
    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(root_remote)], capture_output=True, check=True)
    _committed(root)
    _set_root_origin(root, root_remote.as_uri())
    _embedded_repo(root, "stray")
    # `--no-verify`: this row BUILDS the root a verb must refuse; the pre-commit hook may refuse the setup itself.
    _git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid",
         "commit", "--no-verify", "-q", "-m", "a gitlink nobody declared")
    assert _git_out(root, "ls-tree", "HEAD", "--", "stray").startswith("160000 "), "the stray must be committed"

    rc, out = _cli("store", "push")

    assert rc == 3, out
    assert "undeclared gitlink 'stray' in the root index" in out
    branch = _git_out(root, "symbolic-ref", "--short", "HEAD")
    assert _ls_remote(root_remote.as_uri(), f"refs/heads/{branch}") == "", "nothing may be published"


@pytest.mark.parametrize("spelling", ["./alpha", "alpha/"])
def test_status_never_reports_a_declared_member_spelled_another_way(two_member_ws: Path, spelling: str) -> None:
    root = two_member_ws
    _committed(root)
    _respell_member_path(root, "alpha", spelling)
    assert _git_out(root, "ls-files", "-s", "--", "alpha").startswith("160000 "), "alpha must be a staged gitlink"

    rc, out = _cli("store", "status")

    assert rc == 0, out
    assert "undeclared" not in out
