"""Selected checkout uses committed paths, while ambient readers retain compatibility.

Checkout restores the selected commit's declared physical path and leaves a
legacy name-coordinate checkout unchanged. Ambient materialization, status,
commit and check retain their existing path-then-name compatibility behavior.
A nonempty directory without its own Git root refuses before checkout effects.
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

    # Selected C owns its declared path. The legacy name checkout remains intact.
    why = _identity(root, "alpha-elsewhere", "alpha", placed) + f"\nrc={rc} out={out!r}"
    assert rc == 0, why
    assert _head(placed) == before, why
    assert (root / "alpha" / ".git").is_dir(), why
    expected = _git_out(root, "ls-tree", root_commit, "--", "alpha").split()[2]
    assert _head(root / "alpha") == expected, why


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
    assert rc == 3, why
    assert "not an empty checkout placeholder" in out, why
    assert _head(root) == root_commit, why
    assert (root / "alpha" / "not-a-repo.txt").read_text() == "a plain directory, not a checkout\n"


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


def test_row4_ambient_readers_keep_name_fallback_until_selected_checkout(two_member_ws: Path) -> None:
    """Legacy ambient reads keep their fallback. Selected checkout does not borrow it."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    placed = _place_member_at_its_name(root, "alpha", "alpha-elsewhere")
    assert _cli("store", "commit", "-m", "record at name")[0] == 0
    target = _head(root)
    before = _head(placed)
    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    assert "alpha-elsewhere" in out
    assert not (root / "alpha").exists()
    rc, out = _cli("store", "checkout", target)
    assert rc == 0, out
    assert _head(placed) == before
    assert _head(root / "alpha") == before
