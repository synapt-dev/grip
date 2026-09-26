"""Break attempt 17 — `units[].path` containment. THE HARD GATE.

`spec validate` checked only that a unit path was non-empty and not a file, and
the apply path then did `mkdir` plus a `unit.toml` write at
`workspace_root / unit["path"]` — with none of the containment every other spec
path gets through `canonicalize_workspace_path`. A spec is something people
clone from strangers, so `units[].path` of an absolute path or `../../x` had to
be refused rather than acted on.

Section 6c item 1: a unit path is EXACTLY ONE OF THREE FORMS —
a root-relative path inside the root, `"."`, or exactly `../<single-component>`.
Everything else is refused, naming the unit, with nothing created outside.

Design: the thin-slice design note, section 6c item 1, and
break attempt 17 in section 8.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gr2.python_cli.spec_apply import MaterializationPlanError, unit_member_path, unit_root


# ---------------------------------------------------------------------------
# The three legal forms are accepted
# ---------------------------------------------------------------------------

def test_nested_default_inside_the_root_is_accepted(tmp_path: Path) -> None:
    got = unit_root(tmp_path, {"name": "atlas", "path": "agents/atlas/home"})
    assert got == (tmp_path / "agents" / "atlas" / "home").resolve()


def test_dot_means_the_root_itself(tmp_path: Path) -> None:
    """gr1 `worktree = "main"`: that desk works IN the root."""
    assert unit_root(tmp_path, {"name": "opus", "path": "."}) == tmp_path.resolve()


def test_one_sibling_component_is_accepted(tmp_path: Path) -> None:
    """gr1's real layout: the desk sits BESIDE the root."""
    root = tmp_path / "synapt"
    root.mkdir()
    (tmp_path / "synapt-dev").mkdir()
    got = unit_root(root, {"name": "apollo", "path": "../synapt-dev"})
    assert got == (tmp_path / "synapt-dev").resolve()
    # ...and it is genuinely outside the root, which is the whole point.
    assert root.resolve() not in got.parents


# ---------------------------------------------------------------------------
# Break 17: the four refused forms
# ---------------------------------------------------------------------------

_REFUSED = [
    ("absolute", "/tmp/atlas-break17-absolute"),
    ("parent-escape-twice", "../../x"),
    ("sibling-then-deeper", "../x/y"),
    ("backslash", "..\\x"),
    ("tilde", "~/secrets"),
    ("nul", "a\x00b"),
    ("empty", ""),
    ("dotdot-component", "../.."),
]


@pytest.mark.parametrize("label, path", _REFUSED, ids=[row[0] for row in _REFUSED])
def test_break_17_illegal_unit_paths_are_refused(tmp_path: Path, label: str, path: str) -> None:
    """Each illegal form is refused, and the refusal NAMES THE UNIT.

    A refusal that does not say which unit is a refusal the reader cannot act
    on, which is why the message is asserted and not only the exception type.
    """
    with pytest.raises(MaterializationPlanError) as exc:
        unit_root(tmp_path, {"name": "atlas", "path": path})
    assert "atlas" in str(exc.value), (
        f"[{label}] refusal did not name the unit: {exc.value}"
    )


def test_break_17_a_symlinked_sibling_is_refused(tmp_path: Path) -> None:
    """The escape a `resolve()`-based check structurally cannot catch.

    If the sibling component IS a symlink pointing outside, both the candidate
    and the root resolve through it, so "resolved candidate is under resolved
    root" would hold while the real bytes live somewhere else entirely. The
    per-component lstat walk catches it; a resolve comparison does not.
    """
    root = tmp_path / "synapt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside)

    with pytest.raises(MaterializationPlanError) as exc:
        unit_root(root, {"name": "atlas", "path": "../escape"})
    assert "atlas" in str(exc.value)


def test_break_17_a_symlink_inside_the_root_is_refused(tmp_path: Path) -> None:
    """Same class, nested form: a `..`-free path through a symlinked directory."""
    root = tmp_path / "synapt"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "agents").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(MaterializationPlanError):
        unit_root(root, {"name": "atlas", "path": "agents/atlas/home"})


# ---------------------------------------------------------------------------
# Nothing is written outside the root — fingerprinted, with a planted control
# ---------------------------------------------------------------------------

def _fingerprint(directory: Path) -> str:
    """A recursive digest of a directory: names, kinds and contents."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(b"D" if path.is_dir() else b"F")
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_break_17_nothing_is_written_outside_the_root(tmp_path: Path) -> None:
    """Fingerprint the PARENT, with a planted control file.

    The control is what makes this instrument able to fail: a fingerprint that
    cannot see a file proves nothing about the absence of one, so a file is
    planted and the same fingerprint is required to change when it is.
    """
    parent = tmp_path
    root = parent / "synapt"
    root.mkdir()

    before = _fingerprint(parent)

    # The control: plant a file, require the digest to move.
    probe = parent / "CONTROL-planted"
    probe.write_text("the fingerprint must see this\n")
    with_control = _fingerprint(parent)
    assert with_control != before, (
        "the fingerprint did not change when a file was planted, so it cannot "
        "detect a file written outside the root"
    )
    probe.unlink()

    # Now the attempt. Every illegal path must refuse BEFORE any filesystem act.
    for _label, path in _REFUSED:
        with pytest.raises(MaterializationPlanError):
            unit_root(root, {"name": "atlas", "path": path})

    assert _fingerprint(parent) == before, (
        "the parent directory changed while refusing illegal unit paths"
    )


# ---------------------------------------------------------------------------
# The READ paths translate the refusal instead of leaking a traceback
# ---------------------------------------------------------------------------

def test_break_17_unit_member_path_refuses_in_a_sentence(tmp_path: Path) -> None:
    """A read path meets `unit_root` through a refusal it cannot see coming.

    `unit_member_path` is what lane materialization and the store member map
    call. It must hand the user the sentence `unit_root` already wrote, not a
    Python traceback standing in the place of it — the read verbs walk a spec
    they did not write, and a spec is something people clone from strangers.
    """
    with pytest.raises(SystemExit) as exc:
        unit_member_path(tmp_path, {"name": "atlas", "path": "../../escape"}, "r")
    message = str(exc.value)
    assert "atlas" in message, f"the refusal did not name the unit: {message}"
    assert "escape" in message, f"the refusal did not quote the path: {message}"
    assert "Traceback" not in message


def test_break_17_unit_member_path_control_a_legal_path_resolves(tmp_path: Path) -> None:
    """The control for the row above: a legal unit path RETURNS a path.

    Without it, an instrument that raised on every call would satisfy the
    refusal row while telling us nothing about the translation.
    """
    got = unit_member_path(tmp_path, {"name": "atlas", "path": "agents/atlas/home"}, "r")
    assert got == (tmp_path / "agents" / "atlas" / "home" / "r").resolve()
