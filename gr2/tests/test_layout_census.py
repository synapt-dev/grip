"""The `.grip` path literals in gr2's source, counted by ONE definition, per file, exactly.

WHY THIS EXISTS. The design says the LAYOUT table becomes the only place a gr2 path constant
is defined, and the `.grip*` surface had no single home: four readers counted it four ways
(a Path-chain count of 31, a literal grep of 11, an AST of 6 / 30 / 39) and none was wrong,
because none stated its definition in code. This file states it in code, and holds the
current per-file counts as a RATCHET: every conversion to the layout module lowers one number
here, in the same diff, so the move is visible and cannot quietly reverse.

THE DEFINITION. An `ast.Constant` string with no whitespace (prose, messages and docstrings
carry whitespace and are excluded) containing a path segment that is `.grip`, or begins
`.grip-` / `.grip/`. Walked over the shipped source: every package in the wheel's own list
(`[tool.setuptools] packages` in `pyproject.toml`, resolved through `package-dir`), minus the
named exclusions in EXCLUDED_PACKAGES. The directories are DERIVED from what ships, not picked
by hand: the first version of this census walked two hand-picked trees and never counted
`gr2.prototypes`, which ships in the wheel with 46 literals in 15 files. Tests, venvs and
caches are not source.

THE COUNTS ARE EXACT, NOT CEILINGS. A ceiling that merely may not rise lets a conversion
leave a stale high number behind, and the next literal added to that file then fits under it
unseen. Equality makes the lowering an edit to this table, and makes a new `.grip` literal
in a file this table does not list a red.
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

NOT_SOURCE = {"tests", ".venv", "__pycache__"}
GR2_ROOT = Path(__file__).resolve().parent.parent

#: A shipped package the census does not count, with the reason in one line. Empty on purpose: a
#: package that ships carries layout surface, and a name here is a decision someone has to defend.
EXCLUDED_PACKAGES: dict[str, str] = {}


def shipped_package_dirs(root: Path = GR2_ROOT) -> dict[str, Path]:
    """Package name -> directory, for every package in `[tool.setuptools] packages`, resolved the
    way setuptools does: the longest dotted prefix named in `package-dir` supplies the base, the
    rest of the name is the path under it, and a package with no mapping is a top-level dir."""
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]
    mapping = cfg.get("package-dir", {})
    out: dict[str, Path] = {}
    for pkg in cfg["packages"]:
        parts = pkg.split(".")
        for i in range(len(parts), 0, -1):
            head = ".".join(parts[:i])
            if head in mapping:
                out[pkg] = root / mapping[head] / Path(*parts[i:])
                break
        else:
            out[pkg] = root / Path(*parts)
    return out


def stale_exclusions(root: Path = GR2_ROOT) -> set[str]:
    """Names in EXCLUDED_PACKAGES that are not shipped packages, or that carry no reason: an
    exclusion that outlived its package, or that nobody defended, is a hole with a label on it."""
    shipped = set(shipped_package_dirs(root))
    return {p for p, why in EXCLUDED_PACKAGES.items() if p not in shipped or not why.strip()}


def walked_packages(root: Path = GR2_ROOT) -> set[str]:
    """The shipped packages the census walks: every one that is not named in EXCLUDED_PACKAGES."""
    return set(shipped_package_dirs(root)) - set(EXCLUDED_PACKAGES)

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
    dirs = shipped_package_dirs(root)
    files: set[Path] = set()
    for pkg in walked_packages(root):
        # a subpackage's directory sits inside its parent's: a set counts each file once
        files.update(dirs[pkg].rglob("*.py"))
    counts: dict[str, int] = {}
    for path in sorted(files):
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
    "gr2/prototypes/concurrent_event_stress.py": 4,
    "gr2/prototypes/concurrent_lease_stress.py": 2,
    "gr2/prototypes/concurrent_workspace_cap_stress.py": 3,
    "gr2/prototypes/cross_mode_lane_stress.py": 4,
    "gr2/prototypes/lane_workspace_prototype.py": 10,
    "gr2/prototypes/layout_model_probe.py": 1,
    "gr2/prototypes/python_exec_playground.py": 1,
    "gr2/prototypes/python_hook_runtime_playground.py": 2,
    "gr2/prototypes/python_migration_playground.py": 5,
    "gr2/prototypes/python_review_checkout_playground.py": 3,
    "gr2/prototypes/python_spec_apply_playground.py": 5,
    "gr2/prototypes/real_git_lane_materialization.py": 2,
    "gr2/prototypes/real_git_playground.py": 2,
    "gr2/prototypes/recall_lane_history.py": 1,
    "gr2/prototypes/repo_maintenance_prototype.py": 1,
    "gr2/python_cli/app.py": 8,
    "gr2/python_cli/clone_exec.py": 1,
    "gr2/python_cli/events.py": 2,
    "gr2/python_cli/failures.py": 1,
    "gr2/python_cli/file_exec.py": 1,
    "gr2/python_cli/gitops.py": 1,
    "gr2/python_cli/grip.py": 8,
    "gr2/python_cli/grip_cli.py": 8,
    # the ONE home: GRIP_DIR = ".grip"
    "gr2/python_cli/layout.py": 2,
    "gr2/python_cli/migration.py": 25,
    "gr2/python_cli/open_gr_review.py": 1,
    "gr2/python_cli/pr.py": 1,
    "gr2/python_cli/review_records.py": 3,
    "gr2/python_cli/review_run.py": 4,
    "gr2/python_cli/spec_apply.py": 7,
    "gr2/python_cli/syncops.py": 1,
    "gr2/python_cli/target.py": 1,
    "gr2/python_cli/workspace_guidance.py": 1,
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
    assert "gr2/python_cli/migration.py" in got, "the largest known file is missing from the walk"
    assert "gr2/prototypes/lane_workspace_prototype.py" in got, "gr2.prototypes ships but is not walked"


# -- the walk is derived from what ships ------------------------------------------------------


def test_shipped_package_dirs_resolve_the_real_wheel_the_way_setuptools_does() -> None:
    """Spelled out by hand ON PURPOSE: this is the one row that reds if the resolution rule
    (longest `package-dir` prefix, then the rest of the dotted name; unmapped = a top-level
    directory) is wrong. A package added to the wheel adds a line here, deliberately."""
    root = GR2_ROOT
    assert shipped_package_dirs(root) == {
        "gr2": root / "gr2",
        "gr2.python_cli": root / "gr2" / "python_cli",
        "gr2.prototypes": root / "gr2" / "prototypes",
        "gr2.overlay": root / "gr2" / "overlay",
        "gr2_overlay": root / "gr2_overlay",
    }


def test_every_shipped_package_is_walked_or_named_as_excluded() -> None:
    """The wheel's own package list is the quantifier: nothing that ships escapes the census
    without a name and a reason in EXCLUDED_PACKAGES. Read from pyproject INDEPENDENTLY of
    `shipped_package_dirs`, so a walk that went back to a hand-picked list reds here."""
    cfg = tomllib.loads((GR2_ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    listed = set(cfg["packages"])
    assert walked_packages() | set(EXCLUDED_PACKAGES) == listed
    assert not (walked_packages() & set(EXCLUDED_PACKAGES))
    assert not stale_exclusions(), "an exclusion names an unshipped package or has no reason"
    for pkg in walked_packages():
        assert shipped_package_dirs()[pkg].is_dir(), f"{pkg} ships but its directory is missing"


def _synthetic_wheel(tmp_path: Path) -> Path:
    """A tiny wheel layout: p (mapped to p/), p.sub (inside p/), q (mapped to qdir/), r (UNMAPPED,
    a top-level directory), each with one `.grip` literal, plus a tests/ file that must not
    count."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackages = ["p", "p.sub", "q", "r"]\n'
        '[tool.setuptools.package-dir]\np = "p"\nq = "qdir"\n'
    )
    for rel in ("p/a.py", "p/sub/b.py", "qdir/c.py", "r/d.py", "r/tests/e.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('X = ".grip/state"\n')
    return tmp_path


def test_a_literal_in_any_listed_package_is_counted_and_in_tests_is_not(tmp_path: Path) -> None:
    """The witness for the gap this census had: a new shipped package, a mapped one, an unmapped
    one and a subpackage are all counted once each; a tests/ file is not. Red if the walk is a
    hand list."""
    root = _synthetic_wheel(tmp_path)
    assert census(root) == {"p/a.py": 1, "p/sub/b.py": 1, "qdir/c.py": 1, "r/d.py": 1}


def test_a_stale_or_unexplained_exclusion_is_refused(monkeypatch) -> None:
    """EXCLUDED_PACKAGES is empty today, so this is the only row that can make the check fire."""
    monkeypatch.setitem(EXCLUDED_PACKAGES, "ghost", "a package that no longer ships")
    assert stale_exclusions() == {"ghost"}
    monkeypatch.setitem(EXCLUDED_PACKAGES, "gr2.prototypes", "  ")
    assert stale_exclusions() == {"ghost", "gr2.prototypes"}


def test_an_excluded_package_is_not_counted(tmp_path: Path, monkeypatch) -> None:
    root = _synthetic_wheel(tmp_path)
    monkeypatch.setitem(EXCLUDED_PACKAGES, "q", "a fixture package, not product layout")
    assert "qdir/c.py" not in census(root) and "p/a.py" in census(root)


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
