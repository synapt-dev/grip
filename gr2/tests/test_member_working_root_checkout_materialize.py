"""Rows for the two verbs that still build `root / member["path"]` directly.

`store` resolves a member's WORKING ROOT through `_native_member_working_root` --
the declared PATH unless nothing is at it, in which case the member's NAME
coordinate is tried. Three verbs use it (`status`, `commit`, `check`). Two did
not: `_native_store_checkout` (two sites: the dirty check and the detach) and
`_native_store_materialize` (one site). This file pins what those two do TODAY,
on the unmodified tree, before the conversion.

FOUR ROWS, and each prints the SUBJECT'S IDENTITY on failure -- member, declared
path, and the root each verb actually used -- because the defect this lane is
about is a verb acting on a DIFFERENT directory than the one it reports. A
failure that says only "rc 5" cannot distinguish "the checkout was not restored"
from "the verb looked somewhere else entirely".

Rows 1 and 3 are the two consequences the issue names. Row 2 is the walk-up
case, where the resolved root has no `.git` of its own and git would silently
walk up to an ancestor. Row 4 is the agreement row: after a member is renamed,
five verbs must name ONE root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_store_break_attempts import (  # noqa: E402
    _assert_init_ran,
    _cli,
    _git,
    _git_out,
    _head,
    _set_member_name,
    two_member_ws,
)


def _identity(root: Path, name: str, declared: str, used: Path | None) -> str:
    """The subject's identity, for a failure message. Printed, never asserted on."""
    return (
        f"\n  subject identity:"
        f"\n    member        : {name}"
        f"\n    declared path : {declared}"
        f"\n    declared abs  : {root / declared}"
        f"\n    declared exists: {(root / declared).exists()}"
        f"\n    root the verb used: {used if used is not None else '(not observed)'}"
        f"\n    HEAD of that root : "
        f"{_git_out(used, 'rev-parse', 'HEAD') if used is not None and used.exists() else '(n/a)'}"
    )


def _place_member_at_its_name(ws: Path, member: str, at_name: str) -> Path:
    """Move `member`'s checkout to `at_name` and declare that as its name.

    The shape the fallback exists for: the member is present and healthy one
    directory over, with its DECLARED path empty.
    """
    manifest = ws / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), member, at_name))
    (ws / member).rename(ws / at_name)
    assert not (ws / member).exists(), "the declared path must be empty for this shape"
    assert (ws / at_name / ".git").is_dir(), "the checkout must be where the name says"
    return ws / at_name


def test_row1_checkout_acts_on_a_name_placed_member(two_member_ws: Path) -> None:
    """Row 1. `checkout` on a member whose declared path is EMPTY and whose checkout
    sits at its NAME.

    TODAY this verb builds `root / member['path']`, so the dirty probe and the
    detach both run in a directory that does not exist. `_store_git(..., check=True)`
    raises NativeStoreRefusal, and the verb exits 5 complaining about a path the
    user can see is not the member's checkout. The member is NOT restored, and the
    message does not say where it is.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    first = _head(root / "alpha")
    # A second commit so there is a revision to go back to.
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta second")
    _git(root / "beta", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "record two commits")[0] == 0

    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    before = _head(placed)

    rc, out = _cli("store", "checkout", first[:12])

    # WHAT THE VERB ACTUALLY TOUCHED -- observed, not inferred from rc.
    moved = before != _head(placed)
    why = _identity(root, "alpha", "alpha", placed) + f"\n    rc={rc} out={out[:200]!r}"
    assert rc == 0, (
        "`store checkout` refused a member that is present at its NAME coordinate; "
        "its declared path is empty, which is the shape the resolver exists for." + why
    )
    assert moved, (
        "`store checkout` returned 0 but the checkout at the NAME did not move: the verb "
        "acted on some other directory (the declared path, or a walk-up), not on this "
        "member's working root." + why
    )


def test_row2_checkout_refuses_a_resolved_root_with_no_git(two_member_ws: Path) -> None:
    """Row 2. The WALK-UP case: the declared path is a plain DIRECTORY, and there is
    no checkout at the name either.

    `_native_member_working_root` returns the declared path here -- correctly, since
    it has nothing better to offer. The danger is what git does with it: a plain
    directory inside a repository has no toplevel of its own, so `git status` and
    `git checkout` WALK UP and operate on an ancestor -- the store root and its
    other members. The verb must REFUSE instead, naming the path, because a
    refusal names the state and a walk-up silently answers about a different tree.

    TODAY there is no `.git` check at all, so the walk-up happens.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta second")
    _git(root / "beta", "push", "-q", "origin", "main")
    first = _head(root / "alpha")
    assert _cli("store", "commit", "-m", "record")[0] == 0

    # alpha's checkout moves AWAY entirely; a plain directory takes its declared path.
    (root / "alpha").rename(root / "alpha-moved-away")
    (root / "alpha").mkdir()
    (root / "alpha" / "not-a-repo.txt").write_text("a plain directory, not a checkout\n")

    rc, out = _cli("store", "checkout", first[:12])

    why = _identity(root, "alpha", "alpha", root / "alpha") + f"\n    rc={rc} out={out[:200]!r}"
    assert rc == 3, (
        "a member whose resolved root carries no `.git` must be REFUSED with a named "
        "state (exit 3), not handed to git to walk up from." + why
    )
    assert "alpha" in out, (
        "the refusal must name the member and the path it could not use; a message that "
        "does not name them leaves the reader to work out which verb touched what." + why
    )
    assert "Traceback" not in out, "a refusal is a state, not a crash" + why


def test_row3_materialize_leaves_exactly_one_working_copy_per_member(two_member_ws: Path) -> None:
    """Row 3. `materialize` must leave ONE working copy per member, at the root the
    resolver names -- never a second one cloned at the declared path.

    The message is not the witness: the pre-fix run prints "Materialized 2" at rc 0
    while having cloned a second alpha over at the declared path. So this row COUNTS
    WORKING COPIES, which is what the issue says the message hid.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "record")[0] == 0

    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    placed_head_before = _head(placed)

    rc, out = _cli("store", "materialize")

    # ⚠ COUNT CHECKOUTS, NOT NAMES. The first cut of this row counted directories BY NAME
    # and went GREEN on the unmodified tree while the defect was present: the duplicate
    # materialize creates lands at the DECLARED path (`alpha/`), so a name-based count saw
    # one `alpha` and one `alpha-elsewhere` and called that one working copy. Two
    # checkouts of the same member under two names, counted as one, is the row answering a
    # narrower question than the one it claims -- so the witness is now the two paths the
    # member could be at, and a checkout at the declared path is a failure whichever
    # directory name it carries.
    declared_is_checkout = (root / "alpha" / ".git").exists()
    copies = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / ".git").exists())
    why = (
        _identity(root, "alpha", "alpha", placed)
        + f"\n    rc={rc} out={out[:200]!r}"
        + f"\n    declared path is now a checkout: {declared_is_checkout}"
        + f"\n    checkouts under the root: {copies}"
    )
    assert rc == 0, "materialize should not fail on a name-placed member" + why
    assert not declared_is_checkout, (
        "materialize cloned a SECOND working copy at alpha's DECLARED path while the "
        "member's real checkout sat at its name coordinate. One member, one working "
        "copy, at the root the resolver names." + why
    )
    assert placed.exists(), (
        "the checkout at the NAME was removed rather than adopted; nothing should be moved "
        "or cloned over it." + why
    )
    assert _head(placed) == placed_head_before, (
        "materialize moved the name-placed checkout; it should leave an existing working "
        "copy where it is and only pin HEAD." + why
    )


def test_row4_all_five_verbs_agree_on_one_root_after_a_rename(two_member_ws: Path) -> None:
    """Row 4, the agreement row. After a member is renamed, `checkout`, `status`,
    `commit` and `check` must all name ONE root -- the checkout's.

    This is the row that makes the other three safe to land: converting two verbs
    while three others already resolve differently would leave the group internally
    inconsistent, and the inconsistency would surface as a wrong PIN rather than a
    wrong read.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta second")
    _git(root / "beta", "push", "-q", "origin", "main")
    first = _head(root / "alpha")
    assert _cli("store", "commit", "-m", "record")[0] == 0

    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    placed_before = _head(placed)

    rc_co, out_co = _cli("store", "checkout", first[:12])
    rc_st, out_st = _cli("store", "status", "--json")
    rc_cm, out_cm = _cli("store", "commit", "-m", "after the rename")
    rc_ck, out_ck = _cli("store", "check", "--json")

    why = (
        _identity(root, "alpha", "alpha", placed)
        + f"\n    checkout  rc={rc_co} out={out_co[:160]!r}"
        + f"\n    status    rc={rc_st} out={out_st[:160]!r}"
        + f"\n    commit    rc={rc_cm} out={out_cm[:160]!r}"
        + f"\n    check     rc={rc_ck} out={out_ck[:160]!r}"
    )
    assert rc_co == 0, "checkout must act on the name-placed member" + why
    assert placed_before != _head(placed), (
        "the name-placed checkout did not move, so `checkout` acted elsewhere" + why
    )
    assert rc_st == 0 and rc_cm == 0, "status and commit already resolve the name; they must stay 0" + why
    assert "alpha-elsewhere" in out_st or "alpha" in out_st, (
        "status must report the member it actually read" + why
    )
