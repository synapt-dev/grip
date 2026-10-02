"""Review binds an older gr2 left in `<root>/.grip/.git` move into `refs/dev.synapt.grip/__reviews__/` by themselves.

The migration is AUTOMATIC (no verb to run), runs once before the verb's own
work, says what it did, and keeps every guard: ids keep their shas; each ref is create-only; a rerun
is a no-op; the old store is renamed only after every bind is in the root with an equal tree; any
failure aborts the verb and renames nothing.

The legacy store is the shape the old code made: `<root>/.grip/.git` whose HEAD chains the bind commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.native_root_helper import native_root
from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import _cli, _git_out, two_member_ws  # noqa: F401

_REFS = "refs/dev.synapt.grip/__reviews__/"


def _legacy_binds(ws: Path, tmp_path: Path, n: int = 2) -> list[str]:
    """What an older gr2 left behind: `<root>/.grip/.git` whose HEAD chains `n` bind commits (each
    commit's parent is the one before). The current code no longer writes that shape, so the trees
    are made by binding into a scratch native root and the chain is rebuilt in a fresh repo at
    `.grip/`; the commits are the old shape, the trees are real binds."""
    remote, base, head = _unpushed_head(ws)
    maker = native_root(tmp_path / "legacy-maker")
    rows = [{"key": "alpha", "remote": remote, "path": "alpha", "head": head, "base": base,
             "ref": "refs/heads/main", "title": f"bind {i}", "body": "", "source": str(ws / "alpha")}
            for i in range(n)]
    made = [grip_mod.create_review_bind_commit(maker, [row]) for row in rows]
    legacy = ws / ".grip"
    legacy.mkdir(exist_ok=True)

    def g(*args: str) -> str:
        return subprocess.run(["git", "-C", str(legacy), "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    g("init", "-q")
    g("fetch", "-q", str(maker), "+refs/dev.synapt.grip/__reviews__/*:refs/maker/*")
    chain: list[str] = []
    for commit in made:
        parent = ["-p", chain[-1]] if chain else []
        chain.append(g("commit-tree", f"{commit}^{{tree}}", *parent, "-m", "grip review bind"))
    g("update-ref", "HEAD", chain[-1])
    g("for-each-ref", "--format=%(refname)", "refs/maker")  # the maker refs are dropped below
    for ref in g("for-each-ref", "--format=%(refname)", "refs/maker").splitlines():
        g("update-ref", "-d", ref)
    return chain


def _refs(ws: Path) -> list[str]:
    return [l for l in _git_out(ws, "for-each-ref", "--format=%(refname)", _REFS).splitlines() if l]


def test_old_binds_move_into_refs_on_the_first_bind_touching_verb(two_member_ws: Path, tmp_path: Path, capfd) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    assert len(set(ids)) == 2 and _refs(ws) == []

    code, out = _cli("review", "verify", str(ws), "gr:" + ids[1])
    assert code == 0 and "tree_matches: True" in out, out
    assert "migrated 2 review binds from .grip/.git into refs/dev.synapt.grip/__reviews__" in out + capfd.readouterr().err
    assert sorted(_refs(ws)) == sorted(_REFS + i for i in ids), "ids must keep their shas"
    assert not (ws / ".grip" / ".git").exists() and (ws / ".grip" / "legacy-store.git").is_dir()
    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 0 and "tree_matches: True" in out, out


def test_the_migration_runs_once(two_member_ws: Path, tmp_path: Path, capfd) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    assert _cli("review", "verify", str(ws), "gr:" + ids[0])[0] == 0
    capfd.readouterr()
    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 0 and "migrated" not in out + capfd.readouterr().err


def test_a_failed_migration_aborts_the_verb_and_renames_nothing(two_member_ws: Path, tmp_path: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    (ws / ".grip" / "legacy-store.git").mkdir()  # the rename target is taken

    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 2 and "migration" in out and "legacy-store.git" in out, out
    assert (ws / ".grip" / ".git").is_dir(), "the old store was renamed on a failed migration"
    assert _refs(ws) == [], "refs were made before the guards passed"


def test_an_empty_store_is_moved_aside(two_member_ws: Path, capfd) -> None:
    """A `.grip/.git` that holds no bind and no snapshot is not a store: it goes aside, with one line."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    grip_mod.grip_init(ws)  # an empty alpha store inside a native root
    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2 and "not_bound" in out, out
    assert "moved an empty .grip/.git aside" in out + capfd.readouterr().err
    assert not (ws / ".grip" / ".git").exists() and (ws / ".grip" / "legacy-store.git").is_dir()


def test_a_store_holding_only_snapshots_is_left_where_it_is(two_member_ws: Path) -> None:
    """Real alpha state with no bind is not this migration's to move."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    grip_mod.grip_init(ws)
    grip_mod.create_workspace_commit(
        ws, [{"key": "alpha", "remote": remote, "path": "alpha", "commit": head, "base": base}])
    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2 and "not_bound" in out, out
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
