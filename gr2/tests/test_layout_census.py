"""The `.grip` path literals in gr2's source, counted by ONE definition, per file, exactly.

WHY THIS EXISTS. The design says the LAYOUT table becomes the only place a gr2 path constant
is defined, and the `.grip*` surface had no single home: four readers counted it four ways
(a Path-chain count of 31, a literal grep of 11, an AST of 6 / 30 / 39) and none was wrong,
because none stated its definition in code. This file states it in code, and holds the
current per-file counts as a RATCHET: every conversion to the layout module lowers one number
here, in the same diff, so the move is visible and cannot quietly reverse.

THE DEFINITION. An `ast.Constant` string with no whitespace (prose, messages and docstrings
carry whitespace and are excluded) containing a path segment that is `.grip`, or begins
`.grip-` / `.grip/`. Walked over the shipped source of both trees: `python_cli/` and the
`gr2/` package (the overlay family). Tests, venvs and caches are not source.

THE COUNTS ARE EXACT, NOT CEILINGS. A ceiling that merely may not rise lets a conversion
leave a stale high number behind, and the next literal added to that file then fits under it
unseen. Equality makes the lowering an edit to this table, and makes a new `.grip` literal
in a file this table does not list a red.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SOURCE_TREES = ("python_cli", "gr2")
NOT_SOURCE = {"tests", ".venv", "__pycache__"}
GR2_ROOT = Path(__file__).resolve().parent.parent

#: A path segment that is `.grip`, or `.grip-...` / `.grip/...`. `.gripper` is not one.
GRIP_SEGMENT = re.compile(r"(^|/)\.grip($|[/-])")


def grip_literals(source: str) -> list[str]:
    """Every string constant in `source` that the census counts."""
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and " " not in node.value
        and GRIP_SEGMENT.search(node.value)
    ]


def census(root: Path = GR2_ROOT) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tree in SOURCE_TREES:
        for path in sorted((root / tree).rglob("*.py")):
            if NOT_SOURCE & set(path.relative_to(root).parts):
                continue
            n = len(grip_literals(path.read_text()))
            if n:
                counts[path.relative_to(root).as_posix()] = n
    return counts


#: The ratchet. Lower a number (or delete the row at zero) in the change that converts the
#: file; a file not listed here has none, so a new literal anywhere else is a red.
EXPECTED = {
    "gr2/overlay/activate.py": 1,
    "gr2/overlay/cli.py": 1,
    "gr2/overlay/introspection.py": 1,
    "gr2/overlay/trust.py": 1,
    "gr2/overlay/units.py": 3,
    "gr2/overlay/workspace_spec.py": 1,
    "python_cli/app.py": 9,
    "python_cli/clone_exec.py": 1,
    "python_cli/events.py": 2,
    "python_cli/failures.py": 1,
    "python_cli/file_exec.py": 1,
    "python_cli/gitops.py": 1,
    "python_cli/grip.py": 11,
    "python_cli/grip_cli.py": 8,
    # the ONE home: GRIP_DIR = ".grip"
    "python_cli/layout.py": 1,
    "python_cli/migration.py": 25,
    "python_cli/open_gr_review.py": 1,
    "python_cli/pr.py": 1,
    "python_cli/review_records.py": 3,
    "python_cli/review_run.py": 4,
    "python_cli/spec_apply.py": 7,
    "python_cli/syncops.py": 1,
    "python_cli/target.py": 1,
    "python_cli/workspace_guidance.py": 1,
}


# -- the definition, on synthetic sources: so the census cannot silently change meaning --------


@pytest.mark.parametrize(
    "source, expected",
    [
        ('x = ".grip"', 1),  # the bare name
        ('x = Path(".grip") / "state"', 1),  # a Path chain's first segment
        ('x = ".grip/state/materialization"', 1),  # a rooted subpath
        ('x = ".grip-review-run.json"', 1),  # a sibling file named `.grip-...`
        ('x = "a/.grip/b"', 1),  # a segment in the middle
        ('x = "the .grip dir is private"', 0),  # prose mid-sentence: the regex alone refuses it
        ('x = ".grip/.git is a file, not a directory"', 0),  # prose that STARTS with a path:
        # only the whitespace test refuses it (the real refusal message in grip_cli)
        ('x = ".gripper"', 0),  # a different word
        ('x = "grip.toml"', 0),  # no leading dot
        ('x = f".grip/{name}"', 1),  # an f-string's literal part IS a constant: a site, counted
        ('"""docstring mentioning .grip/state"""', 0),  # a docstring, whitespace again
    ],
)
def test_the_census_definition(source: str, expected: int) -> None:
    assert len(grip_literals(source)) == expected, source


# -- the ratchet -------------------------------------------------------------------------------


def test_the_census_is_not_vacuous() -> None:
    got = census()
    assert sum(got.values()) > 50, "the walk found almost nothing, so this gate cannot fail"
    assert "python_cli/migration.py" in got, "the largest known file is missing from the walk"


def moved(expected: dict[str, int], got: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Files whose count is not EXACTLY the expected one (a file in neither table is zero)."""
    return {
        path: (expected.get(path, 0), got.get(path, 0))
        for path in sorted(set(expected) | set(got))
        if expected.get(path, 0) != got.get(path, 0)
    }


@pytest.mark.parametrize(
    "expected, got, want",
    [
        ({"a.py": 2}, {"a.py": 2}, {}),  # equal: quiet
        ({"a.py": 2}, {"a.py": 3}, {"a.py": (2, 3)}),  # a new literal in a listed file
        ({}, {"b.py": 1}, {"b.py": (0, 1)}),  # a new literal in an UNLISTED file
        ({"a.py": 4}, {}, {"a.py": (4, 0)}),  # a STALE HIGH row after a conversion: this is
        # the case a ceiling would pass, and the reason the counts are exact
        ({"a.py": 4}, {"a.py": 3}, {"a.py": (4, 3)}),  # partly converted, number not lowered
    ],
)
def test_the_ratchet_is_exact_not_a_ceiling(expected, got, want) -> None:
    assert moved(expected, got) == want


def test_the_per_file_counts_are_exactly_the_ratchet() -> None:
    got = census()
    drift = moved(EXPECTED, got)
    assert not drift, (
        "`.grip` path literals moved (file: expected -> found). A NEW literal belongs in "
        "gr2/python_cli/layout.py; a CONVERSION lowers its number in EXPECTED in the same "
        f"change:\n  " + "\n  ".join(f"{p}: {a} -> {b}" for p, (a, b) in drift.items())
    )


# -- the one home ------------------------------------------------------------------------------


def test_grip_dir_is_the_on_disk_name() -> None:
    """Changing this string moves every existing workspace's state: it is a layout row's
    spelling, not a free constant."""
    from gr2.python_cli.layout import GRIP_DIR, grip_dir

    assert GRIP_DIR == ".grip"
    assert grip_dir(Path("/ws")) == Path("/ws") / ".grip"


def test_consent_paths_are_unchanged_by_the_move() -> None:
    """THE BEHAVIOUR PIN for the file converted in this change: every path consent.py
    builds is byte-identical to the one it built from the literal, spelled here WITHOUT the
    constant so a wrong GRIP_DIR reds this row too."""
    from gr2.python_cli import consent

    root = Path("/ws")
    assert consent.consent_path(root, "member/sub") == root / ".grip" / "consent" / "member/sub.json"
    assert consent.workspace_spec_path(root) == root / ".grip" / "workspace_spec.toml"
    assert consent.PENDING_DIR == Path(".grip") / "state" / "hooks_pending"
    assert consent.pending_marker_path(root, "m") == root / ".grip" / "state" / "hooks_pending" / "m.json"
