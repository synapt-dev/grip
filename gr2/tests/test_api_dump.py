"""The two gates on gr2's committed public-surface dump.

- ``test_api_dump_is_current``: the committed ``api/cli.api`` must equal what
  the generator renders now. A change to the public surface fails here unless
  the file changes in the same PR.
- ``test_api_stable_share_at_least_90``: stable items over all non-reserved,
  non-hidden items must be at least 0.90, with both counts printed. The lever
  is hiding our internal process verbs, not freezing everything: hidden items
  leave the denominator, which is why the hide list is load-bearing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
CLI_API = GR2 / "api" / "cli.api"
GENERATOR = GR2 / "scripts" / "dump_api.py"

# Population floors, one per kind. Without them the two gates above pass on a
# dump that has lost whole kinds: mutating the generator's parameter loop to
# iterate over nothing drops this file from 414 items to 85 -- every flag and
# every positional gone -- and both gates still report success, because the
# share gate only ever divides what is left and the currency gate only compares
# the file to whatever the generator now renders. A witness that cannot notice
# a shrunken subject is not measuring the subject.
#
# These are population ALARMS, not ratchets: each sits under the count at the
# change that introduced it, so a deliberate small removal still lands, and
# what makes a removal visible in review is the currency gate forcing the
# `.api` file to change in the same PR.
#
# What they do NOT catch is drift inside the margin -- flags can still go
# unnoticed one at a time -- and that is the deliberate trade. A floor bounds
# catastrophic per-kind loss, which is the failure that reads as success.
MIN_VERBS = 70
MIN_FLAGS = 175
MIN_ARGS = 95


def _generator():
    """Load the generator by path -- it is a script, not an installed module."""
    spec = importlib.util.spec_from_file_location("_gr2_dump_api", GENERATOR)
    assert spec and spec.loader, f"cannot load {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(text: str) -> list[tuple[str, str, str]]:
    """(kind, label, marker) for every line, parsed from the two fixed columns."""
    rows = []
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        label, _, marker = rest.rstrip().rpartition(" ")
        rows.append((kind, label.strip(), marker))
    return rows


def test_api_dump_is_current() -> None:
    rendered = _generator().render()
    assert rendered.strip(), (
        "the generator rendered nothing; an empty dump must never compare equal "
        "to a committed file, or two empty things read as current"
    )
    committed = CLI_API.read_text() if CLI_API.exists() else ""
    assert committed == rendered, (
        f"{CLI_API} is not current.\n"
        f"  regenerate: python scripts/dump_api.py\n"
        f"  then name the public-surface change in the PR body.\n"
        f"  committed lines={len(committed.splitlines())} "
        f"rendered lines={len(rendered.splitlines())}"
    )


def test_api_stable_share_at_least_90() -> None:
    rows = _rows(_generator().render())
    counted = [
        (kind, label, marker)
        for kind, label, marker in rows
        if marker != "reserved" and "(hidden" not in label
    ]
    stable = [row for row in counted if row[2] == "stable"]
    share = len(stable) / len(counted) if counted else 1.0
    print(f"stable share: {len(stable)}/{len(counted)} = {share:.4f}")
    assert share >= 0.90, (
        f"stable share {len(stable)}/{len(counted)} = {share:.4f} is under 0.90.\n"
        f"  There are levers here and they are not interchangeable:\n"
        f"    hidden     an internal verb goes hidden=True and leaves the denominator\n"
        f"    reserved   a reserved name leaves the denominator the same way\n"
        f"    may-change the item STAYS in the denominator and stops counting as stable\n"
        f"  Use may-change only for surface that is public and genuinely still moving,\n"
        f"  and note the two are not interchangeable: taking a MAY-CHANGE item out of\n"
        f"  the denominator raises this ratio, and taking a STABLE one out lowers it.\n"
        f"  Hiding a family you would have marked may-change is what makes the surface\n"
        f"  smaller in the direction this gate measures."
    )


def test_stability_entries_all_name_a_real_item() -> None:
    """Every stability entry must match an item the dump actually carries.

    An entry that names nothing is SILENT: a typo, or one left behind by a
    renamed verb, marks no line, the regenerated file comes out byte-identical,
    and the file reads as applied while the item it meant to mark is untouched.
    Every other gate here passes on it, so this is the only place it can be
    caught.
    """
    rows = _rows(_generator().render())
    known = set()
    for kind, label, _ in rows:
        known.add((kind, label.split(" (hidden")[0].strip()))
    entries = _generator()._markers()
    missing = sorted(f"{kind} {spelling}" for (kind, spelling) in entries if (kind, spelling) not in known)
    assert not missing, (
        "api/stability.toml entries naming no item in the dump (their marker is "
        "silently never applied):\n  " + "\n  ".join(missing)
    )


def test_api_dump_has_a_population() -> None:
    """Each kind must still be populated, so a silent loss of one cannot pass.

    This is the floor the other two gates lack: the share gate divides whatever
    is left, and the currency gate compares the file to whatever is rendered,
    so a generator that stopped emitting flags and positionals would satisfy
    both. Measured: the parameter loop mutated to iterate over nothing takes
    the dump from 414 items to 85 and both gates stay green.
    """
    rows = _rows(_generator().render())
    verbs = [r for r in rows if r[0] == "verb"]
    flags = [r for r in rows if r[0] == "flag"]
    args = [r for r in rows if r[0] == "arg"]
    print(f"population: {len(verbs)} verbs, {len(flags)} flags, {len(args)} positionals")
    assert len(verbs) >= MIN_VERBS, f"only {len(verbs)} verbs (floor {MIN_VERBS})"
    assert len(flags) >= MIN_FLAGS, f"only {len(flags)} flags (floor {MIN_FLAGS})"
    assert len(args) >= MIN_ARGS, f"only {len(args)} positionals (floor {MIN_ARGS})"
