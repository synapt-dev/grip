"""A `store commit` with nothing to record is a no-op, not a failure.

THE DEFECT, measured on grip dev f072cf45: `_native_store_commit` ends with `git commit`
after staging. When every pin already equals the gitlink the root last committed there is
nothing staged, git exits 1 with "nothing to commit" on STDOUT and 0 bytes on stderr, and
`_store_git` turns an empty stderr into the bare "git command failed". The verb prefixes it
and exits 5, the "cannot measure" row, for a stranger's perfectly ordinary second commit.
Two older test comments name it as a separate finding (test_store_log_adoption_boundary,
test_member_working_root_checkout_materialize).

THE SPECIFIED SHAPE: an unchanged store commit exits 0, says so, makes no commit, and in
`--json` reports status `unchanged` with the root's current HEAD.

THE CONTROL IS THE LOAD-BEARING ROW. A fix that exits 0 on EVERY commit would pass the
no-op rows, so `test_a_real_change_is_still_recorded` pushes a member commit to its origin
first (store commit records only origin-covered pins) and requires a NEW root commit, the
`committed` status, and a moved gitlink.

Fixtures are real git with bare local origins and no network, the helpers of the smoke file.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _root(tmp_path: Path) -> Path:
    """A gr1-shaped root: two member clones side by side, the root not yet a repo."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
    return root


def _recorded_root(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    assert gr2(root, "init").returncode == 0
    first = gr2(root, "commit", "-m", "first")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    return root


def _head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def _count(root: Path) -> int:
    return int(git(root, "rev-list", "--count", "HEAD").stdout.strip())


def _gitlink(root: Path, member: str) -> str:
    return git(root, "ls-tree", "HEAD", "--", member).stdout.split()[2]


def test_a_second_commit_with_nothing_to_record_exits_zero_and_says_so(tmp_path: Path) -> None:
    root = _recorded_root(tmp_path)
    head, count = _head(root), _count(root)

    second = gr2(root, "commit", "-m", "second", check=False)
    out = second.stdout + second.stderr

    assert second.returncode == 0, f"a no-op is not a failure\n{out}"
    assert "git command failed" not in out, out
    assert "cannot complete" not in out, out
    assert "nothing to record" in out.lower(), f"the verb must say what happened\n{out}"
    # THE FRUIT: no commit was made.
    assert _head(root) == head
    assert _count(root) == count == 1


def test_json_reports_unchanged_with_the_current_head(tmp_path: Path) -> None:
    root = _recorded_root(tmp_path)
    head = _head(root)

    second = gr2(root, "commit", "-m", "second", "--json", check=False)

    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    payload = json.loads(second.stdout)
    assert payload == {"status": "unchanged", "root_commit": head}, payload


def test_a_real_change_is_still_recorded(tmp_path: Path) -> None:
    """THE CONTROL: without it, "always exit 0, never commit" would pass every row above."""
    root = _recorded_root(tmp_path)
    before_head, before_count = _head(root), _count(root)
    before_link = _gitlink(root, "alpha")

    work = root / "alpha"
    configure_identity(work)
    (work / "moved.txt").write_text("member moved\n")
    git(work, "add", "moved.txt")
    git(work, "commit", "-m", "member moved")
    git(work, "push", "origin", "main")

    second = gr2(root, "commit", "-m", "second", "--json")
    payload = json.loads(second.stdout)

    assert payload["status"] == "committed", payload
    assert _count(root) == before_count + 1
    assert _head(root) != before_head
    assert payload["root_commit"] == _head(root)
    assert _gitlink(root, "alpha") != before_link
    assert _gitlink(root, "alpha") == git(work, "rev-parse", "HEAD").stdout.strip()

    # And the very next commit, with nothing new, is the no-op again.
    third = gr2(root, "commit", "-m", "third", "--json")
    assert json.loads(third.stdout) == {"status": "unchanged", "root_commit": _head(root)}


def test_an_untracked_root_file_alone_is_still_unchanged(tmp_path: Path) -> None:
    """The verb stages `grip.toml`, the generated allow-list and gitlinks, never the root's
    other files, so a stray file is not "something to record"."""
    root = _recorded_root(tmp_path)
    head = _head(root)
    (root / "scratch-notes.txt").write_text("not part of the store\n")

    second = gr2(root, "commit", "-m", "second", "--json", check=False)

    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert json.loads(second.stdout) == {"status": "unchanged", "root_commit": head}
    assert _head(root) == head


def test_the_hidden_snapshot_alias_is_a_no_op_too(tmp_path: Path) -> None:
    """`store snapshot` is a hidden alias of `store commit` (design section 5)."""
    root = _recorded_root(tmp_path)
    head = _head(root)

    second = gr2(root, "snapshot", "-m", "again", "--json", check=False)

    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert json.loads(second.stdout) == {"status": "unchanged", "root_commit": head}
    assert _head(root) == head
