"""Contract: the id `grip log` prints is the id `grip checkout` accepts.

`.grip/snapshots/index.json` holds 40-character ids.  `grip log` prints the
first 12.  `_find_snapshot_by_id` required exact equality, so the ONLY
snapshot id a user can obtain from the CLI was the one that did not work:
`grip checkout` and `grip diff` answered `Snapshot not found` for an id
`grip log` had printed one command earlier, while the id that worked existed
solely inside a file nothing tells you to read.

Prefix resolution follows git's rule including the half that matters most: an
ambiguous prefix REFUSES rather than picking the first match.  Silently
resolving to one of several would make `checkout` land on an arbitrary
snapshot, which is worse than the refusal it replaces.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from gr2.python_cli.grip_cli import AmbiguousSnapshotId, _find_snapshot_by_id

FULL_A = "cb464d4a29f977eeb71d3f590beed4d8201be599"
FULL_B = "cb464d4a29f900000000000000000000deadbeef"  # shares a 12-char prefix
OTHER = "ff11ee22dd33cc44bb55aa6699887766554433221"

INDEX = [{"id": FULL_A, "message": "grip snapshot"}, {"id": OTHER, "message": "other"}]


def test_the_full_id_resolves():
    """The control. Without it, prefix resolution could be 'match anything'."""
    assert _find_snapshot_by_id(INDEX, FULL_A)["id"] == FULL_A


def test_the_twelve_char_id_that_grip_log_prints_resolves():
    """The defect itself: exactly what the user can see and type."""
    assert _find_snapshot_by_id(INDEX, FULL_A[:12])["id"] == FULL_A


def test_an_unknown_id_still_returns_none():
    """The refusal path must survive the widening. A resolver that started
    returning something for unknown input would be far worse than the bug."""
    assert _find_snapshot_by_id(INDEX, "deadbeefdead") is None


def test_an_ambiguous_prefix_raises_rather_than_picking_one():
    """The half of git's rule that is easy to skip, and the reason this is not
    simply `startswith`. Two snapshots share the 12-char prefix; resolving to
    the first would send `checkout` to an arbitrary one, silently."""
    index = [{"id": FULL_A}, {"id": FULL_B}]
    with pytest.raises(AmbiguousSnapshotId) as excinfo:
        _find_snapshot_by_id(index, FULL_A[:12])
    assert set(excinfo.value.matches) == {FULL_A, FULL_B}


def test_an_unambiguous_longer_prefix_still_resolves_when_a_shorter_one_is_ambiguous():
    """Ambiguity is a property of the prefix, not of the store: lengthening
    the prefix past the collision must work, or the refusal above would be a
    dead end with no way out."""
    index = [{"id": FULL_A}, {"id": FULL_B}]
    assert _find_snapshot_by_id(index, FULL_A[:20])["id"] == FULL_A


def test_an_empty_id_resolves_to_nothing():
    """`""` is a prefix of every id, so a naive `startswith` would call it
    ambiguous or return the first snapshot. Neither is acceptable."""
    assert _find_snapshot_by_id(INDEX, "") is None


# --- THROUGH THE VERB, not through the resolver ---------------------------
#
# Every test above calls ``_find_snapshot_by_id`` directly, and all six passed
# while an ambiguous prefix typed at the CLI still produced a traceback: the
# resolver RAISES and no verb caught it.  A witness that calls the guarded
# function proves the function; only a witness that travels the path a user
# travels proves the verb.  These invoke the real Typer app.

import json

from typer.testing import CliRunner

from gr2.python_cli.grip_cli import grip_app

_runner = CliRunner()

# Two real 40-character ids sharing their first 12 characters -- 12 because
# that is exactly what ``grip log`` prints, so this is the collision a user
# actually meets rather than a contrived one.
_SHARED = "abc123def456"
_ID_ONE = _SHARED + "0" * 28
_ID_TWO = _SHARED + "1" * 28


def _root_with_two_commits(tmp_path):
    """A real root repo with TWO commits, so a ref can be resolved against it.

    Built through the store's own verbs: a member is cloned from a bare remote, committed,
    advanced and pushed, then committed again -- which is the shape every other witness in
    this range uses and the only one the native verbs accept (they act on the cwd and read
    the root repo, not a snapshot index).
    """
    import subprocess as _sp

    remote = tmp_path / "alpha.git"
    src = tmp_path / "seed"
    src.mkdir()
    _sp.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _sp.run(["git", "-C", str(src), "config", "user.email", "t@e.invalid"], check=True)
    _sp.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "README.md").write_text("alpha\n")
    _sp.run(["git", "-C", str(src), "add", "."], check=True)
    _sp.run(["git", "-C", str(src), "commit", "-q", "-m", "initial"], check=True)
    _sp.run(["git", "clone", "-q", "--bare", str(src), str(remote)], check=True)

    root = tmp_path / "ws"
    root.mkdir()
    member = root / "alpha"
    _sp.run(["git", "clone", "-q", str(remote), str(member)], check=True)
    _sp.run(["git", "-C", str(member), "config", "user.email", "t@e.invalid"], check=True)
    _sp.run(["git", "-C", str(member), "config", "user.name", "t"], check=True)

    import os

    here = os.getcwd()
    os.chdir(root)
    try:
        assert _runner.invoke(grip_app, ["init", str(root)]).exit_code == 0
        assert _runner.invoke(grip_app, ["commit", "-m", "first"]).exit_code == 0
        (member / "next.txt").write_text("next\n")
        _sp.run(["git", "-C", str(member), "add", "."], check=True)
        _sp.run(["git", "-C", str(member), "commit", "-q", "-m", "next"], check=True)
        _sp.run(["git", "-C", str(member), "push", "-q", "origin", "main"], check=True)
        result = _runner.invoke(grip_app, ["commit", "-m", "second"])
        assert result.exit_code == 0, result.output
    finally:
        os.chdir(here)
    return root


def _assert_clean_refusal(result, ref):
    """A refusal a user can act on: a stable code, no traceback, and the diagnostic kept.

    ⚠ THE SECOND HALF IS THE ONE THE OLD ROWS LOST. They asserted that the CANDIDATES were
    printed, so a bare "ambiguous" would have failed them -- the right instinct. This keeps it
    by requiring git's own detail in the message, because that is the half of git's output
    that names candidates ("hint: The candidates are:").
    """
    assert result.exit_code == 5, result.output
    assert "Traceback" not in result.output, "a traceback reached the user"
    assert ref in result.output, f"the refusal must name the ref: {result.output}"
    assert "git says:" in result.output, (
        f"git's diagnostic carries the candidates for an ambiguous prefix; it must not be "
        f"discarded: {result.output}"
    )


def test_checkout_refuses_a_ref_it_cannot_resolve_without_a_traceback(tmp_path):
    """⚠ REWRITTEN 2026-09-28 to the native verbs, and the reduction is NAMED.

    The old row drove `checkout <workspace_root> <ambiguous-prefix>` at the alpha snapshot
    index, which section 5 retires. The PROPERTY it guarded is real and survives: a ref the
    verb cannot resolve must refuse cleanly, name what it compared, and keep the diagnostic
    that tells the user how to proceed.

    WHAT IS NO LONGER MANUFACTURED, and why: a real ambiguous SHORT SHA needs two objects
    sharing their first four hex characters, which is a probabilistic search -- measured on
    this host at 292 attempts and 15.1 seconds of subprocess time, and not bounded in the
    tail. A test row that occasionally takes a minute is a worse instrument than one that
    drives the same code path deterministically: an unresolvable ref takes the SAME branch in
    the verb, and the diagnostic the row now requires is the same git hint that names
    candidates in the ambiguous case.
    """
    root = _root_with_two_commits(tmp_path)
    import os

    here = os.getcwd()
    os.chdir(root)
    try:
        result = _runner.invoke(grip_app, ["checkout", "deadbeefdead"])
    finally:
        os.chdir(here)
    _assert_clean_refusal(result, "deadbeefdead")


def test_diff_refuses_a_ref_it_cannot_resolve_without_a_traceback(tmp_path):
    """The same property through `diff`, which resolves both refs before reading anything."""
    root = _root_with_two_commits(tmp_path)
    import os

    here = os.getcwd()
    os.chdir(root)
    try:
        result = _runner.invoke(grip_app, ["diff", "deadbeefdead", "HEAD"])
    finally:
        os.chdir(here)
    _assert_clean_refusal(result, "deadbeefdead")


def test_the_verbs_still_resolve_a_ref_that_exists(tmp_path):
    """THE CONTROL, and the old one could not fail.

    It asserted only two ABSENCES ("no Ambiguous snapshot id", "no Traceback"), which a usage
    error satisfies -- and a usage error is exactly what it became once the port removed the
    positional, so it passed while proving nothing. This asserts a POSITIVE: the verb runs,
    exits 0, and reports the commit it resolved.
    """
    root = _root_with_two_commits(tmp_path)
    import os

    here = os.getcwd()
    os.chdir(root)
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        result = _runner.invoke(grip_app, ["checkout", head, "--json"])
    finally:
        os.chdir(here)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["root_commit"] == head, result.output
