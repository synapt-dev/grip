"""Rows for a `store checkout` that fails after detaching the root.

The verb detaches the root's HEAD and THEN reads the member pins, so a failure in the member loop
leaves the workspace HALF-APPLIED: the root sits at the target commit while every member still sits
at its old pin. The failure itself is an uncaught `KeyError` from the pin lookup, which reaches the
caller as `exit 1` with empty stdout AND empty stderr -- and the next verb then fails too.

The agreed fix shape is PHASE ONE BEFORE ANY WRITE: resolve every pin from the snapshot at the target
revision, resolve every member's working root, and run every check that can refuse, so that a refusal
before the first mutation leaves root and members BYTE-IDENTICAL. That is what rows 1 and 2 pin. It
does NOT make phase two atomic -- a git error on the third member can still leave members in
different states -- and row 4 is about that case, where the requirement is that the output NAMES
which members moved and which did not rather than exiting bare.

THE TRIGGER IS A RENAME BETWEEN THE TARGET REVISION AND THE MANIFEST, and the checkout is left at
its DECLARED path throughout, so there is no declared-path gap here. That isolation is deliberate:
it is what makes these rows a different defect from the working-root resolution one, and it is why
converting the working-root sites does not touch this.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_store_break_attempts import (  # noqa: E402
    _assert_init_ran,
    _cli,
    _git,
    _git_out,
    _head,
    _set_member_name,
    two_member_ws,
)


def _invoke(*args: str):
    """The raw CliRunner result, not `_cli`'s (rc, output) pair.

    `CliRunner` catches the exception into `result.exception` and leaves the output EMPTY, so a row
    built on `_cli` alone cannot tell "the verb refused with a message" from "the verb raised and
    said nothing" -- which is precisely the defect here. This returns the result itself so a row can
    assert on stdout, stderr and the exception separately.
    """
    return make_cli_runner().invoke(app, list(args))


def _renamed_but_not_moved(root: Path, member: str, new_name: str) -> str:
    """Rename a member in the manifest and COMMIT it, leaving its checkout where it is.

    Returns the PREVIOUS root commit, which is the target revision whose snapshot still carries the
    OLD name. Committing matters twice over: the root must be clean before the run, or the verb
    refuses for the root's dirtiness and the run teaches nothing; and the two revisions must differ
    by the NAME alone, which is what keeps this off the declared-path defect.
    """
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), member, new_name))
    assert _git_out(root, "status", "--porcelain"), (
        "the renamed manifest must be a CHANGE the root has not recorded, or the rename is not a "
        "difference between the two revisions at all"
    )
    assert _cli("store", "commit", "-m", "record the rename")[0] == 0
    assert _git_out(root, "status", "--porcelain") == "", (
        "the root must be CLEAN before the checkout under test; a dirty root refuses for the "
        "root's reason and the row would be measuring that instead"
    )
    return new_name


def _store_with_two_members(root: Path) -> str:
    """A clean two-member store, returning the root commit whose snapshot carries the OLD names."""
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    for name in ("alpha", "beta"):
        _git(root / name, "commit", "-q", "--allow-empty", "-m", f"{name} second")
        _git(root / name, "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "first record")[0] == 0
    first_record = _head(root)
    _renamed_but_not_moved(root, "alpha", "alpha-renamed")
    return first_record


def _symref(repo: Path) -> str:
    """The branch HEAD points at, or a named "detached" marker.

    ⚠ `check=False` MATTERS: on a detached HEAD `git symbolic-ref` exits 128, so a `check=True` read
    raises CalledProcessError WHILE BUILDING a failure message -- the row dies in its own diagnostic
    instead of reporting, which is how the first cut of this file failed. And the empty answer is
    itself the witness, so it is reported as a word rather than as an empty string.
    """
    out = _git(repo, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    return out or "(detached)"


def _heads(root: Path) -> dict[str, str]:
    heads = {"root": _head(root)}
    for name in ("alpha", "beta"):
        if (root / name).is_dir():
            heads[name] = _git_out(root / name, "rev-parse", "HEAD")
    return heads


def test_row1_selected_commit_ignores_later_ambient_rename(two_member_ws: Path) -> None:
    """A later name does not replace the target commit's complete member tuple."""
    root = two_member_ws
    target = _store_with_two_members(root)
    before = _heads(root)
    res = _invoke("store", "checkout", target[:12])
    assert res.exit_code == 0, res.output
    assert _head(root) == target
    assert _symref(root) == "(detached)"
    assert _head(root / "alpha") == before["alpha"]
    assert _head(root / "beta") == before["beta"]


def test_row2_dirty_selected_member_refuses_before_root_movement(two_member_ws: Path) -> None:
    root = two_member_ws
    target = _store_with_two_members(root)
    (root / "alpha" / "untracked-refusal").write_text("dirty")
    before, branch = _heads(root), _symref(root)
    res = _invoke("store", "checkout", target[:12])
    assert res.exit_code == 3, res.output
    assert "alpha is dirty" in res.output
    assert _heads(root) == before and _symref(root) == branch


@pytest.mark.parametrize("extra", [[], ["--json"]], ids=["plain", "json"])
def test_row3_plain_and_json_name_the_same_prewrite_refusal(two_member_ws: Path, extra: list[str]) -> None:
    root = two_member_ws
    target = _store_with_two_members(root)
    (root / "alpha" / "untracked-refusal").write_text("dirty")
    before = _heads(root)
    res = _invoke("store", "checkout", target[:12], *extra)
    assert res.exit_code == 3, res.output
    assert "alpha is dirty" in res.output
    assert _heads(root) == before


def test_row4_a_phase_two_failure_names_the_members_that_moved(two_member_ws: Path) -> None:
    """Row 4. Phase two is NOT atomic, and when it fails the output must NAME the state.

    The agreed boundary: the shape refuses before the first write, which covers everything knowable
    in advance — but phase two still mutates, and a git error on the third member can leave the
    members in different states. The requirement for that case is not a rollback (deliberately not
    built here: a rollback is itself a second mutation that can fail halfway, which is the same
    class as the half-applied state it would repair) but a plain line that says which members moved,
    which did not, and where the root now sits.

    Asserted on the NAMED MEMBERS rather than on the exit code, because an exit code alone passes for
    any failure at all — including one that says nothing about the state it left behind.

    THE INJECTION IS A READ-ONLY `.git` ON THE SECOND MEMBER: the phase-one dirty probe still reads
    that repository fine, so the run reaches phase two, and `git checkout --detach` then fails
    writing HEAD. The mode is restored in a `finally` so a failed assertion cannot leave the fixture
    unwritable for whatever runs next.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    for name in ("alpha", "beta"):
        _git(root / name, "commit", "-q", "--allow-empty", "-m", f"{name} second")
        _git(root / name, "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "first record")[0] == 0
    target = _head(root)
    # A second commit per member, so the checkout has somewhere to restore them FROM.
    for name in ("alpha", "beta"):
        _git(root / name, "commit", "-q", "--allow-empty", "-m", f"{name} third")
        _git(root / name, "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second record")[0] == 0
    assert _git_out(root, "status", "--porcelain") == "", "the root must start clean"

    before = _heads(root)
    beta_git = root / "beta" / ".git"
    # ⚠ THE INJECTION IS PORTABLE, WHICH IS WHY IT IS NOT A READ-ONLY MODE. A read-only `.git` stops
    # a checkout only for a NON-ROOT user on a filesystem that honours the mode, so it needs a
    # precondition probe and a loud skip to stay honest. Replacing the member's `.git` with a FRESH
    # repository satisfies both halves of what phases one and two require in OPPOSITE directions: the
    # tree is CLEAN, so the dirty probe lets it through, and the pinned commit is NOT resolvable, so
    # phase two's checkout fails. No permissions, no skip, any user, any filesystem.
    #
    # `git add -A` IS LOAD-BEARING AND NOT DECORATION: a fresh `git init` leaves the working files
    # UNTRACKED, so `status --porcelain` reads dirty and the run refuses in PHASE ONE -- which is not
    # the failure this row is about. The first attempt at this forgot it, and the assert below is
    # that half of the precondition.
    shutil.rmtree(beta_git)
    _git(root / "beta", "init", "-q", "-b", "main")
    _git(root / "beta", "config", "user.email", "t@e.invalid")
    _git(root / "beta", "config", "user.name", "t")
    _git(root / "beta", "add", "-A")
    _git(root / "beta", "commit", "-q", "-m", "a fresh repository over the same tree")
    assert _git_out(root / "beta", "status", "--porcelain") == "", (
        "the injected member must be CLEAN, or the run refuses in phase one and this row measures "
        "the wrong failure"
    )
    # AND THE PIN MUST BE UNRESOLVABLE, asserted rather than assumed, so a change that accidentally
    # makes it resolvable reports that instead of rendering a verdict from a run that never failed.
    probe = _git(root / "beta", "rev-parse", "--verify", f"{before['beta']}^{{commit}}", check=False)
    assert probe.returncode != 0, (
        "the injected member must not resolve the pin it is to be checked out at; if it can, no "
        f"phase-two failure can occur. probe stderr: {probe.stderr.strip()!r}"
    )
    res = _invoke("store", "checkout", target[:12])
    after = _heads(root)

    out = (res.stdout or "") + (res.stderr or "")
    moved = [n for n in ("alpha", "beta") if before.get(n) != after.get(n)]
    why = (
        f"\n    target revision : {target[:12]}"
        f"\n    member heads before: {before}"
        f"\n    member heads after : {after}"
        f"\n    MOVED members   : {moved}"
        f"\n    root HEAD after : {after['root']}  symref={_symref(root)!r}"
        f"\n    rc={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
        f"\n    exception={res.exception!r}"
        f"\n    injection probe : rc={probe.returncode} stderr={probe.stderr.strip()!r}"
    )
    assert res.exit_code != 0, "a member that could not be checked out must not report success" + why
    assert moved, (
        "this row is only meaningful if phase two actually moved an EARLIER member before the "
        "injection failed the later one; with nothing moved it is row 1's case again." + why
    )
    for name in moved:
        assert name in out, (
            f"the failure must NAME the member that moved ({name!r}); a caller cannot tell which "
            "parts of the workspace were applied from an exit code." + why
        )
    for name in ("alpha", "beta"):
        if name not in moved:
            assert name in out, (
                f"the failure must NAME the member that did NOT move ({name!r}), or the reader "
                "cannot tell a half-applied workspace from a fully applied one." + why
            )


def test_row5_a_dirty_member_is_refused_before_the_root_moves(two_member_ws: Path) -> None:
    """Row 5. The DIRTY PROBE's position — which no other row pins.

    Rows 1 to 3 all refuse at the PIN LOOKUP: their trigger is a member renamed since the target
    revision. Between them they therefore pin where the pin lookup sits and NOTHING ELSE. Moving
    only the dirty probe from phase one into phase two — leaving the working-root resolution and the
    pin lookup exactly where the fix put them — leaves every one of those rows GREEN while the root
    moves before the refusal. That is the half-applied state, reached through a trigger no other row
    uses, and it was found by asking whether the rows could pass with the ordering wrong rather than
    by reading them.

    The fixture makes the ordering observable: both members have moved on since the target, so a run
    that reaches the detach really does move the root, and exactly ONE member is dirty, so the
    refusal is attributable to it.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    for name in ("alpha", "beta"):
        # A TRACKED file, created here rather than assumed from the fixture, so this row knows what
        # to dirty and cannot silently dirty nothing.
        (root / name / "tracked.txt").write_text("one\n")
        _git(root / name, "add", "-A")
        _git(root / name, "commit", "-q", "-m", f"{name} first")
        _git(root / name, "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "first record")[0] == 0
    target = _head(root)
    # EVERY MEMBER MOVES ON, so restoring to `target` is real work and not a no-op: with the pins
    # equal to the live heads the detach would change nothing and this row could not tell a
    # correctly ordered verb from a wrongly ordered one.
    for name in ("alpha", "beta"):
        (root / name / "tracked.txt").write_text("two\n")
        _git(root / name, "add", "-A")
        _git(root / name, "commit", "-q", "-m", f"{name} second")
        _git(root / name, "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second record")[0] == 0
    # ONE MEMBER DIRTY, uncommitted, and the other left clean.
    (root / "alpha" / "tracked.txt").write_text("dirty, uncommitted\n")
    assert _git_out(root / "alpha", "status", "--porcelain") != "", "alpha must be dirty for this row"
    assert _git_out(root / "beta", "status", "--porcelain") == "", "beta must stay clean"

    before_root = _head(root)
    before_heads = _heads(root)
    res = _invoke("store", "checkout", target[:12])
    after_root = _head(root)
    after_heads = _heads(root)

    out = (res.stdout or "") + (res.stderr or "")
    why = (
        f"\n    target revision : {target[:12]}"
        f"\n    root HEAD before: {before_root}"
        f"\n    root HEAD after : {after_root}   symref={_symref(root)!r}"
        f"\n    member heads before: {before_heads}"
        f"\n    member heads after : {after_heads}"
        f"\n    rc={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
        f"\n    exception={res.exception!r}"
    )
    assert res.exit_code != 0, "a dirty member cannot be restored" + why
    assert after_root == before_root, (
        "the dirty member must be refused BEFORE the root is detached. The root MOVED, which is the "
        "half-applied state arrived at through the dirty probe rather than the pin lookup — the "
        "ordering no other row in this file pins." + why
    )
    assert after_heads == before_heads, "no member may move on a refusal" + why
    assert "alpha" in out, "the refusal must name the dirty member" + why
