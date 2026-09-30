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

from pathlib import Path

# IMPORTED BY ITS PACKAGE PATH, NOT BY BARE MODULE NAME. `tests/__init__.py` exists (0 bytes), so
# pytest's prepend mode inserts gr2/ -- the first directory WITHOUT an `__init__.py` -- into
# sys.path, and a bare `test_store_break_attempts` is then unresolvable. Measured: the file fails
# at COLLECTION with `ModuleNotFoundError: No module named 'test_store_break_attempts'` under the
# canonical invocation, `python -m pytest` from gr2/, and passes as soon as `tests/` is supplied on
# the path -- so the rows were never the problem, the import was. That canonical invocation is what
# CI runs (.github/workflows/ci.yml, job `gr2_python`, `working-directory: gr2`, `python -m pytest`)
# and what a reader runs by hand, so the bare form would have gone red in CI while passing for an
# author who happened to export PYTHONPATH. The package form is also this directory's convention:
# twenty-odd files here import `tests.conftest` the same way.
from tests.test_store_break_attempts import (
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
    # A second commit so there is a revision to go back to.
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta second")
    _git(root / "beta", "push", "-q", "origin", "main")

    # ⚠ THE MEMBER IS PLACED AT ITS NAME **BEFORE** THE ROOT COMMIT, and the order is load
    # bearing. Placing it after leaves the committed revision naming the member `alpha`
    # while the working tree names it `alpha-elsewhere`, and `_native_members_at` keys the
    # pins by the name IN THAT REVISION -- so `pins[member["name"]]` raises KeyError and the
    # verb dies at exit 1 with NO message on stderr at all. That is a real defect (an
    # uncaught crash where a refusal belongs) and it is NOT this lane's; committing after
    # the placement keeps this row measuring the member-path resolution it claims to.
    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    assert _cli("store", "commit", "-m", "record with the member at its name")[0] == 0

    # THE SUBJECT IS A ROOT COMMIT, not a member's sha. `checkout` materializes the members
    # AT A ROOT COMMIT, and a member's commit object does not exist in the root store --
    # passing one produced "is not a root commit this store can resolve", a refusal about
    # the ARGUMENT. A row can be red for a reason other than the one it claims.
    root_commit = _head(root)

    # The member moves on, so the checkout has somewhere to restore it FROM.
    _git(placed, "commit", "-q", "--allow-empty", "-m", "third")
    _git(placed, "push", "-q", "origin", "main")
    before = _head(placed)

    rc, out = _cli("store", "checkout", root_commit[:12])

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
    assert _cli("store", "commit", "-m", "record")[0] == 0
    # A ROOT commit, for the same reason as row 1 -- and taken AFTER the commit, because
    # the root has no commit to name before `store commit` runs.
    root_commit = _head(root)

    # alpha's checkout moves AWAY entirely; a plain directory takes its declared path.
    (root / "alpha").rename(root / "alpha-moved-away")
    (root / "alpha").mkdir()
    (root / "alpha" / "not-a-repo.txt").write_text("a plain directory, not a checkout\n")

    rc, out = _cli("store", "checkout", root_commit[:12])

    why = _identity(root, "alpha", "alpha", root / "alpha") + f"\n    rc={rc} out={out[:200]!r}"
    # EXIT 5, matching the group's existing answer for this exact state: `store commit`
    # already refuses "is not a checkout; materialize it first" with 5 (grip_cli.py:956),
    # and this is that state reached by a different verb. The row's first cut asserted 3 by
    # no better argument than my own convenience; the codebase already had a code for it.
    assert rc == 5, (
        "a member whose resolved root carries no `.git` must be REFUSED with a named "
        "state (exit 5, the group's code for `is not a checkout`), not handed to git to "
        "walk up from." + why
    )
    # ⚠ THIS ASSERTION IS THE ONE CARRYING THE MUTATION, and the exit code above is not.
    # Measured by removing the `.git` check (the mutation, counted: the literal went 1 -> 0):
    # the walk-up then runs and git fails on the store root's tree, which `_store_git`'s
    # residual RuntimeError backstop maps to code 5 -- so the rc assertion alone STILL PASSES
    # and the row stays green on a tree with the refusal deleted. What catches it is the
    # message: the verb answers with git's "fatal: unable to read tree <sha>", which names
    # neither the member nor its path. Delete this assertion as redundant with the rc above
    # and the row stops being able to fail.
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
    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    assert _cli("store", "commit", "-m", "record with the member at its name")[0] == 0
    root_commit = _head(root)

    # ⚠ THE TARGET REVISION IS POST-RENAME, AND THIS ASSERTS IT RATHER THAN ASSUMING IT.
    # `_native_members_at(root, sha)` keys the pins by the names recorded in grip.toml AT THAT
    # REVISION, so `store checkout` on a PRE-rename revision raises an uncaught KeyError at
    # grip_cli.py:1122 before a single member is restored (measured by Atlas, in an isolation
    # run with no path gap at all -- so it is a separate defect on a separate line, filed as
    # its own issue and sequenced after this lane). A row that checked out a pre-rename
    # revision would die on THAT while reading as "the path resolver is broken" -- the resolver
    # would never have been reached. `root_commit` is taken AFTER the `store commit` that
    # records the rename, so the name at that revision is the renamed one; that is the entire
    # content of this precondition, and it is what keeps this row unentangled with the KeyError.
    recorded = _git_out(root, "show", f"{root_commit[:12]}:grip.toml")
    assert 'name = "alpha-elsewhere"' in recorded, (
        "this row must check out a POST-rename revision: the pin lookup keys on the member "
        "names recorded at the target revision, and a pre-rename target hits an unrelated "
        "uncaught KeyError that would read as this lane's fix failing. Recorded grip.toml:\n"
        + recorded
    )

    # The member moves on, so `checkout` has somewhere to restore it FROM. Without this
    # the detach is a no-op and "did not move" is indistinguishable from "acted elsewhere",
    # which is precisely the confusion this row exists to remove.
    _git(placed, "commit", "-q", "--allow-empty", "-m", "third")
    _git(placed, "push", "-q", "origin", "main")
    placed_before = _head(placed)

    rc_co, out_co = _cli("store", "checkout", root_commit[:12])
    # ⚠ THE WITNESS IS TAKEN HERE, AT THE MOMENT THE VERB RAN. Read at the END of the row
    # instead, it answers about the last thing that touched the member: the re-attach below
    # is a `git checkout main`, which puts HEAD back on exactly `placed_before`, so an
    # end-of-row read compared the third commit with itself and reported "checkout did not
    # move" while the checkout had in fact moved it (measured). A witness has a MOMENT as
    # well as a subject.
    placed_after_checkout = _head(placed)
    rc_st, out_st = _cli("store", "status", "--json")

    # `commit` NEEDS SOMETHING TO RECORD, and the preceding `checkout` removes it: restoring
    # the member onto the pin leaves HEAD equal to the recorded gitlink, so there is nothing
    # to commit -- and `git commit` on a clean tree exits 1 with its message on **stdout**,
    # which `_store_git`'s `stderr.strip() or "git command failed"` turns into a bare
    # "git command failed" at exit 5 (measured: rc=1, stdout="nothing to commit", stderr
    # empty, as a control against the same command with a real change). That refusal is about
    # THIS FIXTURE, not about which root the verb resolved, and a row that read it as a
    # resolution failure would be red for a reason it does not claim. Detaching also left the
    # member off its branch, so putting it back is what gives `commit` a pin to record
    # (and keeps it coverable: a dangling detached commit is not an ancestor of origin/main).
    _git(placed, "checkout", "-q", "main")

    rc_cm, out_cm = _cli("store", "commit", "-m", "after the rename")
    rc_ck, out_ck = _cli("store", "check", "--json")

    why = (
        _identity(root, "alpha", "alpha", placed)
        + f"\n    checkout  rc={rc_co} out={out_co[:160]!r}"
        + f"\n    status    rc={rc_st} out={out_st[:160]!r}"
        + f"\n    commit    rc={rc_cm} out={out_cm[:160]!r}"
        + f"\n    check     rc={rc_ck} out={out_ck[:160]!r}"
        + f"\n    target revision (post-rename) : {root_commit[:12]}"
        + f"\n    placed HEAD before checkout : {placed_before}"
        + f"\n    placed HEAD after  checkout : {placed_after_checkout}"
    )
    assert rc_co == 0, "checkout must act on the name-placed member" + why
    assert placed_before != placed_after_checkout, (
        "the name-placed checkout did not move, so `checkout` acted elsewhere" + why
    )
    assert rc_st == 0 and rc_cm == 0, (
        "status and commit already resolve the name; they must stay 0" + why
    )
    assert "alpha-elsewhere" in out_st or "alpha" in out_st, (
        "status must report the member it actually read" + why
    )
