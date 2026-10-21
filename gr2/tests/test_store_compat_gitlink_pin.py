"""Compat mode: the gitlink is the member pin and the grip.toml pin line is a derived copy (cut 1, derive).

- A hand commit that moves the gitlink but not the line: log and diff report the gitlink, and push refuses while
  HEAD's line and gitlink disagree, naming the member, both pins and `store commit` as the fix.
- A hand edit that moves the line but not the gitlink: nothing reads it (the working members, log, diff, the
  committed pin all keep the gitlink).
- Full mode keeps reading the line, as before this cut.
"""
from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip_cli as gc
from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import SHADOW, _advance, _refs, _store


def _line(root: Path, rev: str, member: str) -> str:
    block = git(root, "show", f"{rev}:grip.toml").stdout.split(f'name = "{member}"', 1)[1].split("[[members]]", 1)[0]
    return block.split('pin = "', 1)[1].split('"', 1)[0]


def _link(root: Path, rev: str, member: str) -> str:
    return git(root, "ls-tree", rev, "--", member).stdout.split()[2]


def _hand_line(root: Path, member: str) -> tuple[str, str]:
    """Advance ``member`` and write its new head into the working grip.toml pin line only. Returns (old, new)."""
    old = _line(root, "HEAD", member)
    _advance(root, member)
    new = git(root / member, "rev-parse", "HEAD").stdout.strip()
    toml = root / "grip.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(f'pin = "{old}"', f'pin = "{new}"', 1), encoding="utf-8")
    return old, new


def _pins(entries: list[dict], member: str) -> list[str]:
    return [change["after"] for entry in entries for change in entry["pins"] if change["name"] == member]


def test_compat_hand_commit_moves_the_gitlink_and_log_diff_push_report_the_gitlink(tmp_path):
    root, origin = _store(tmp_path, "compat")
    gr2(root, "push")
    shadow, remote = _refs(root)[SHADOW], _refs(origin)
    old = _line(root, "HEAD", "alpha")
    _advance(root, "alpha")
    git(root, "add", "alpha")
    git(root, "commit", "--no-verify", "-m", "hand bump past the hook")
    new = _link(root, "HEAD", "alpha")
    assert new != old and _line(root, "HEAD", "alpha") == old, "the gitlink moved, the line did not"

    assert _pins(gc._native_store_log(root, 10), "alpha")[-1] == new, "log reports the gitlink"
    diff = {row["name"]: row for row in gc._native_store_diff(root, "HEAD~1", "HEAD")}
    assert (diff["alpha"]["old_pin"], diff["alpha"]["new_pin"], diff["alpha"]["changed"]) == (old, new, True)
    assert diff["beta"]["changed"] is False

    result = gr2(root, "push", check=False)

    assert result.returncode == 4, (result.returncode, result.stderr)
    assert "gitlink is not the pin grip.toml records" in result.stderr, result.stderr
    assert f"alpha (gitlink {new[:12]}, pin {old[:12]})" in result.stderr, result.stderr
    assert "store commit`" in result.stderr, result.stderr
    assert _refs(origin) == remote and _refs(root)[SHADOW] == shadow, "nothing moves"


def test_compat_hand_edit_moves_the_line_and_nothing_reads_it(tmp_path):
    root, _ = _store(tmp_path, "compat")
    link = _link(root, "HEAD", "alpha")
    old, new = _hand_line(root, "alpha")
    assert old == link

    working = {m["name"]: m["pin"] for m in gc._native_members(root)}
    assert working["alpha"] == link, "the working members read the staged gitlink, not the edited line"
    assert gc._committed_pin(root, {"name": "alpha", "path": "alpha", "pin": new}) == link

    git(root, "add", "grip.toml")
    git(root, "commit", "--no-verify", "-m", "hand line past the hook")
    assert _line(root, "HEAD", "alpha") == new and _link(root, "HEAD", "alpha") == link

    assert gc._native_members_at(root, "HEAD")["alpha"] == link
    assert gc._committed_pin(root, {"name": "alpha", "path": "alpha", "pin": new}) == link, "HEAD's gitlink, not line"
    assert new not in _pins(gc._native_store_log(root, 10), "alpha"), "log never reports the line"
    diff = {row["name"]: row for row in gc._native_store_diff(root, "HEAD~1", "HEAD")}
    assert (diff["alpha"]["new_pin"], diff["alpha"]["changed"]) == (link, False), diff


def test_full_mode_answers_are_unchanged_the_line_is_still_read(tmp_path):
    root, _ = _store(tmp_path, "full")
    old, new = _hand_line(root, "alpha")

    assert {m["name"]: m["pin"] for m in gc._native_members(root)}["alpha"] == new, "full: the working line, as dev"
    assert gc._committed_pin(root, {"name": "alpha", "path": "alpha", "pin": new}) == old, "full: HEAD's line"

    git(root, "add", "grip.toml")
    git(root, "commit", "--no-verify", "-m", "hand line past the hook")

    assert gc._native_members_at(root, "HEAD")["alpha"] == new, "full: the committed line, as dev"
    diff = {row["name"]: row for row in gc._native_store_diff(root, "HEAD~1", "HEAD")}
    assert (diff["alpha"]["old_pin"], diff["alpha"]["new_pin"], diff["alpha"]["changed"]) == (old, new, True)
