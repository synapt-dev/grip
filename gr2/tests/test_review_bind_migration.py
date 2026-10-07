"""Review binds an older gr2 left in `<root>/.grip/.git` move into `the review namespace` by themselves.

The migration is AUTOMATIC (no verb to run), runs once before the verb's own
work, says what it did, and keeps every guard: ids keep their shas; each ref is create-only; a rerun
is a no-op; the old store is renamed only after every bind is in the root with an equal tree; any
failure aborts the verb and renames nothing.

The legacy store is the shape the old code made: `<root>/.grip/.git` whose HEAD chains the bind commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.review_ref_helper import REVIEW_REF_ROOT, legacy_bind_tree
from gr2.python_cli import grip as grip_mod

from tests.native_root_helper import native_root
from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import _cli, _git_out, two_member_ws  # noqa: F401

_REFS = REVIEW_REF_ROOT


def _legacy_binds(ws: Path, tmp_path: Path, n: int = 2) -> list[str]:
    """What an older gr2 left behind: `<root>/.grip/.git` whose HEAD chains `n` bind commits (each
    commit's parent is the one before). The current code no longer writes that shape, so the trees
    are built independently as old v2 records in a scratch native root, and the chain is rebuilt
    in a fresh repo at `.grip/`; a new writer cannot change this legacy fixture."""
    remote, base, head = _unpushed_head(ws)
    maker = native_root(tmp_path / "legacy-maker")
    rows = [{"key": "alpha", "remote": remote, "path": "alpha", "head": head, "base": base,
             "ref": "refs/heads/main", "title": f"bind {i}", "body": "", "source": str(ws / "alpha")}
            for i in range(n)]
    trees = [legacy_bind_tree(maker, row) for row in rows]
    legacy = ws / ".grip"
    legacy.mkdir(exist_ok=True)

    def g(*args: str) -> str:
        return subprocess.run(["git", "-C", str(legacy), "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    g("init", "-q")
    # Import each immutable legacy tree through a commit, not a namespace glob
    # that could mix two ref versions.
    for tree in trees:
        made = _git_out(maker, "-c", "user.name=t", "-c", "user.email=t@e.invalid",
                        "commit-tree", tree, "-m", "legacy fixture")
        g("fetch", "-q", str(maker), made)
    chain: list[str] = []
    for tree in trees:
        parent = ["-p", chain[-1]] if chain else []
        chain.append(g("commit-tree", tree, *parent, "-m", "grip review bind"))
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
    assert f"migrated 2 review binds from .grip/.git into {REVIEW_REF_ROOT.rstrip(chr(47))}" in out + capfd.readouterr().err
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


def test_a_taken_aside_name_is_never_overwritten_and_the_next_free_one_is_used(two_member_ws: Path, tmp_path: Path) -> None:
    """`store migrate` and an earlier migration both leave `legacy-store.git`; a later `.grip/.git`
    must neither wedge the verbs nor overwrite that copy."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    taken = ws / ".grip" / "legacy-store.git"
    taken.mkdir()
    (taken / "keep").write_text("an earlier aside copy")

    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 0 and "tree_matches: True" in out, out
    assert (taken / "keep").read_text() == "an earlier aside copy", "the earlier aside copy was overwritten"
    assert (ws / ".grip" / "legacy-store-2.git").is_dir() and not (ws / ".grip" / ".git").exists()
    assert sorted(_refs(ws)) == sorted(_REFS + i for i in ids)


def test_a_bind_whose_tree_differs_in_the_root_aborts_and_renames_nothing(
    two_member_ws: Path, tmp_path: Path, monkeypatch
) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    real = grip_mod._bind_git

    def lying(workspace, *args):
        if args and args[0] == "rev-parse" and any(a.endswith("^{tree}") for a in args):
            return subprocess.CompletedProcess(args, 0, stdout="0" * 40 + "\n", stderr="")
        return real(workspace, *args)

    monkeypatch.setattr(grip_mod, "_bind_git", lying)
    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 2 and "migration" in out and "tree is" in out, out
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
    assert _refs(ws) == [], "refs were made before the guards passed"


def test_a_bind_that_cannot_rederive_its_own_tree_aborts_and_renames_nothing(
    two_member_ws: Path, tmp_path: Path, monkeypatch
) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    monkeypatch.setattr(grip_mod, "_verify_review_commit_in_store", lambda workspace, commit: {"tree_matches": False})
    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 2 and "does not re-derive its own tree" in out, out
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
    assert _refs(ws) == []


def test_a_store_with_snapshots_above_its_binds_keeps_its_snapshots(two_member_ws: Path, tmp_path: Path, capfd) -> None:
    """The snapshot verbs still read `.grip/.git`: the binds are copied into refs and the store stays."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    ids = _legacy_binds(ws, tmp_path)
    legacy = ws / ".grip"

    def g(*args: str, stdin: str | None = None) -> str:
        return subprocess.run(["git", "-C", str(legacy), "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
                              input=stdin, capture_output=True, text=True, check=True).stdout.strip()

    schema_blob = g("hash-object", "-w", "--stdin", stdin=grip_mod._WORKSPACE_SCHEMA)
    grip_tree = g("mktree", stdin=f"100644 blob {schema_blob}\tschema\n")
    root_tree = g("mktree", stdin=f"040000 tree {grip_tree}\t.grip\n")
    snapshot = g("commit-tree", root_tree, "-p", ids[-1], "-m", "grip workspace snapshot")
    g("update-ref", "HEAD", snapshot)
    assert len(grip_mod.grip_log(ws)) >= 1

    code, out = _cli("review", "verify", str(ws), "gr:" + ids[0])
    assert code == 0 and "tree_matches: True" in out, out
    assert "stays, it also holds alpha snapshots" in out + capfd.readouterr().err
    assert sorted(_refs(ws)) == sorted(_REFS + i for i in ids)
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
    assert snapshot in [c.commit if hasattr(c, "commit") else c.sha for c in grip_mod.grip_log(ws)], "the snapshot store lost its snapshot"


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
