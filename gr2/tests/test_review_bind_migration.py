"""Review binds an older gr2 left in `<root>/.grip/.git` move into `refs/dev.synapt.grip/__reviews__/` by themselves.

The migration is AUTOMATIC (no verb to run), runs once before the verb's own
work, says what it did, and keeps every guard: ids keep their shas; each ref is create-only; a rerun
is a no-op; the old store is renamed only after every bind is in the root with an equal tree; any
failure aborts the verb and renames nothing.

The legacy store is made the way the old code made it: an alpha root (`grip_init`) bound twice, so the
two commits are CHAINED, then its `.git` is moved under a native root's `.grip/`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import _cli, _git_out, two_member_ws  # noqa: F401

_REFS = "refs/dev.synapt.grip/__reviews__/"


def _legacy_binds(ws: Path, tmp_path: Path, n: int = 2) -> list[str]:
    remote, base, head = _unpushed_head(ws)
    alpha_root = tmp_path / "legacy-maker"
    alpha_root.mkdir()
    grip_mod.grip_init(alpha_root)
    row = {"key": "alpha", "remote": remote, "path": "alpha", "head": head, "base": base,
           "ref": "refs/heads/main", "title": "", "body": "", "source": str(ws / "alpha")}
    ids = [grip_mod.create_review_bind_commit(alpha_root, [row]) for _ in range(n)]
    (ws / ".grip").mkdir(exist_ok=True)
    shutil.move(str(alpha_root / ".grip" / ".git"), str(ws / ".grip" / ".git"))
    return ids


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


def test_a_store_with_no_review_bind_is_left_alone(two_member_ws: Path) -> None:
    """An alpha snapshot-only (or empty) `.grip/.git` is not this migration's to move."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    grip_mod.grip_init(ws)  # an empty alpha store inside a native root
    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2 and "not_bound" in out, out
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
