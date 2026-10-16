"""`gr2 review receive` never migrates the pinned reviews an older gr2 left in `<root>/.grip/.git`.

Receive publishes the one review it fetched and nothing else: the old store's refs are identical
after it, the old store keeps its name, and none of its binds appears under the review namespace.
The verbs that migrate keep doing so (test_review_bind_migration.py).
"""
from __future__ import annotations

import subprocess

from tests.review_ref_helper import REVIEW_REF_ROOT, legacy_bind_tree
from tests.test_review_transport import _handoff, cli, git, publish


def _legacy_store(root, remote, base, head) -> list[str]:
    """`<root>/.grip/.git` whose HEAD chains one old v2 bind, the shape an older gr2 left."""
    legacy = root / ".grip"
    legacy.mkdir(exist_ok=True)
    git(legacy, "init", "-q")
    tree = legacy_bind_tree(legacy, {"key": "member", "remote": str(remote), "path": "member",
                                     "head": head, "base": base, "title": "old bind"})
    commit = git(legacy, "commit-tree", tree, "-m", "grip review bind")
    git(legacy, "update-ref", "HEAD", commit)
    return [commit]


def _show_ref(store) -> str:
    return subprocess.run(["git", "--git-dir", str(store), "show-ref", "--head"],
                          capture_output=True, text=True).stdout


def _namespace(root) -> set[str]:
    return set(git(root, "for-each-ref", "--format=%(refname)", REVIEW_REF_ROOT).split())


def test_receive_leaves_old_location_pins_untouched(tmp_path, monkeypatch):
    author, receiver, remote, commit, base, head = _handoff(tmp_path, monkeypatch)
    publish((author, receiver, remote, commit, base, head), monkeypatch)
    old = _legacy_store(receiver, remote, base, base)
    before = _show_ref(receiver / ".grip" / ".git")
    assert before

    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)

    assert result.exit_code == 0, result.output
    assert (receiver / ".grip" / ".git").is_dir(), "receive renamed the old store"
    assert not (receiver / ".grip" / "legacy-store.git").exists()
    assert _show_ref(receiver / ".grip" / ".git") == before
    assert not {r for r in _namespace(receiver) if any(r.endswith(c) for c in old)}, "receive migrated a pin"
    assert "migrated" not in result.output


def test_receive_into_a_new_location_store_as_today(tmp_path, monkeypatch):
    author, receiver, remote, commit, base, head = _handoff(tmp_path, monkeypatch)
    publish((author, receiver, remote, commit, base, head), monkeypatch)

    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)

    assert result.exit_code == 0, result.output
    assert any(r.endswith(commit) for r in _namespace(receiver))
    assert not (receiver / ".grip" / ".git").exists()
