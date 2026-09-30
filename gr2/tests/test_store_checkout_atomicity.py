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


def test_row1_a_refusal_before_the_first_write_moves_nothing(two_member_ws: Path) -> None:
    """Row 1. The atomicity row: a refusal leaves root and members BYTE-IDENTICAL.

    Today the root is detached BEFORE the member loop raises, so the root ends at the target commit
    while both members still sit at their old pins -- a workspace the caller did not ask for and
    cannot see. The witness is the before/after pair, not the exit code: an exit code alone cannot
    distinguish "refused and changed nothing" from "refused and moved the root".
    """
    root = two_member_ws
    target = _store_with_two_members(root)

    before = _heads(root)
    root_symbolic_before = _symref(root)

    res = _invoke("store", "checkout", target[:12])

    after = _heads(root)
    why = (
        f"\n    target revision : {target[:12]}"
        f"\n    root HEAD before: {before['root']}"
        f"\n    root HEAD after : {after['root']}"
        f"\n    root symbolic-ref before: {root_symbolic_before!r}"
        f"\n    root symbolic-ref after : {_symref(root)!r}"
        f"\n    member heads before: {before}"
        f"\n    member heads after : {after}"
        f"\n    rc={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
        f"\n    exception={res.exception!r}"
    )
    assert after["root"] == before["root"], (
        "the verb refused, and a refusal must leave the root where it found it. Today the root is "
        "detached before the member loop raises, so it ends at the target while the members stay "
        "behind -- the half-applied state this row exists for." + why
    )
    assert after["alpha"] == before["alpha"] and after["beta"] == before["beta"], (
        "a refusal must leave every member where it found it too; a member that moved while the "
        "verb refused is the same half-applied state one layer down." + why
    )


def test_row2_the_refusal_is_named_and_does_not_escape_as_an_exception(two_member_ws: Path) -> None:
    """Row 2. The failure is the group's own named refusal, not an uncaught traceback.

    `pins[member["name"]]` raises `KeyError` for a member renamed since the target revision, and it
    is raised rather than converted to a `NativeStoreRefusal` -- so it escapes the verb's error
    shape entirely. The row asserts the POSITIVE property (a message naming what could not be
    resolved) rather than the absence of a traceback, because an empty output would satisfy the
    absence and is exactly what the defect produces.
    """
    root = two_member_ws
    target = _store_with_two_members(root)

    res = _invoke("store", "checkout", target[:12])

    out = (res.stdout or "") + (res.stderr or "")
    why = (
        f"\n    target revision : {target[:12]}"
        f"\n    rc={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
        f"\n    exception={res.exception!r}"
    )
    assert res.exit_code != 0, (
        "a member whose name is absent from the target snapshot cannot be restored, so this must "
        "not report success." + why
    )
    assert out.strip(), (
        "the verb produced NO diagnostic at all -- empty stdout and empty stderr is the measured "
        "symptom of the uncaught KeyError, and a caller has nothing to act on." + why
    )
    assert "Traceback" not in out, (
        "a refusal is a state, not a crash; an uncaught exception escaping the verb's error shape "
        "is the defect this row pins." + why
    )
    # A CONVERTED REFUSAL IS STILL A `SystemExit` under `CliRunner` -- the group's refusal is
    # surfaced as an exit carrying its code, which is the shape working as designed. The defect is
    # a RAW exception escaping instead (the `KeyError` this row was written for), which reaches the
    # caller as exit 1 with nothing on either stream.
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        "the failure must be converted to the group's own refusal, not escape to the caller as a "
        f"raw exception. Escaped: {res.exception!r}" + why
    )
    assert "alpha" in out, (
        "the refusal must name the member it could not resolve, or the reader is left to work out "
        "which one failed." + why
    )


@pytest.mark.parametrize("extra", [[], ["--json"]], ids=["plain", "json"])
def test_row3_json_carries_the_same_diagnostic_as_the_plain_path(
    two_member_ws: Path, extra: list[str]
) -> None:
    """Row 3. `--json` must not be a second, quieter contract.

    MEASURED ON THIS TRIGGER, and it corrects an assumption I brought in from the issue: BOTH paths
    are silent, not only `--json`. The issue records the plain path printing a backstop diagnostic
    ("fatal: unable to read tree") -- that was a DIFFERENT trigger, where the root had already been
    detached and a member's checkout failed. On the renamed-member trigger this row uses, the
    `KeyError` escapes before either path prints anything, so the plain path is silent too. Stated
    because the first draft of this docstring called the plain path "the control", and a control that
    is itself broken is not a control.

    The property is therefore asserted of BOTH paths, on its own terms: a refusal must produce a
    diagnostic naming the member, on whichever path the caller chose. A `--json` returning nothing is
    the worst case for a parser -- it cannot tell "refused" from "crashed" from "printed nothing" --
    and a plain path returning nothing has the same defect for a human.

    PARITY IS PINNED BY CONSTRUCTION, not by comparing two runs in one test: the same assertions run
    against both invocations, and each PARAMETRISATION gets its own fixture instance. Asking for
    `two_member_ws` twice inside one test would hand back the SAME store -- the fixture is
    function-scoped -- and since the first run detaches the root, the second would be measuring a
    workspace the first one had already altered. Two runs, two workspaces.
    """
    root = two_member_ws
    target = _store_with_two_members(root)

    res = _invoke("store", "checkout", target[:12], *extra)
    out = (res.stdout or "") + (res.stderr or "")

    why = (
        f"\n    args: {' '.join(extra) or '(plain)'}"
        f"\n    rc={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
        f"\n    exception={res.exception!r}"
    )
    assert res.exit_code != 0, "a member absent from the target snapshot cannot be restored" + why
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        "the failure must be converted to the group's own refusal on BOTH paths -- a raw exception "
        f"escaping one of them is the defect. Escaped: {res.exception!r}" + why
    )
    assert out.strip(), (
        "this path produced NOTHING while the other produces a diagnostic: a caller parsing "
        "`--json` cannot tell a refusal from a crash from silence." + why
    )
    assert "alpha" in out, (
        "the diagnostic must name the member it could not resolve on BOTH paths, or the two paths "
        "disagree about what happened." + why
    )


def test_row4_a_phase_two_failure_names_the_members_that_moved(two_member_ws: Path) -> None:
    """Row 4. Phase two is NOT atomic, and when it fails the output must NAME the state.

    Apollo's boundary: the agreed shape refuses before the first write, which covers everything
    knowable in advance — but phase two still mutates, and a git error on the third member can leave
    the members in different states. The requirement for that case is not a rollback (deliberately
    not built here: a rollback is itself a second mutation that can fail halfway, which is the same
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
    mode = beta_git.stat().st_mode
    os.chmod(beta_git, 0o555)
    try:
        # ⚠ THE INJECTION ASSERTS ITS OWN PRECONDITION, because it is PERMISSION-SHAPED: a
        # read-only `.git` stops a checkout only for a NON-ROOT user on a filesystem that honours
        # the mode. As root (a container CI job) or on a filesystem that ignores it, the checkout
        # SUCCEEDS, phase two never fails, and this row would render a verdict on the fix from a run
        # that never exercised it. So the fault is PROBED directly first, and a run where it did not
        # take says exactly that instead of reporting on the fix.
        #
        # CI: `.github/workflows/ci.yml`'s `gr2_python` job carries no `container:` and no `user:`
        # (READ, not run here), so it runs as the default non-root GitHub-hosted runner user and the
        # injection can take there.
        probe = _git(root / "beta", "checkout", "--detach", "HEAD", check=False)
        if probe.returncode == 0:
            pytest.skip(
                f"injection did not take: a read-only .git did not stop a checkout "
                f"(euid={os.geteuid()}), so no phase-two failure can be produced on this host. "
                f"This is a SKIP, never a pass -- the fault never happened, so the row has nothing "
                f"to say about the fix. Probe stderr: {probe.stderr.strip()!r}"
            )
        res = _invoke("store", "checkout", target[:12])
    finally:
        os.chmod(beta_git, mode)
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
