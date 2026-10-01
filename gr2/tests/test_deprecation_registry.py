"""The deprecation-alias registry and its two gates.

`gr2/api/deprecations.toml` is the ONE place a REGISTERED deprecated name is
recorded, and it carries ONE expiry vocabulary -- a MILESTONE (`alpha` / `beta` /
`release`), never a version number. Read its header for why; the short form is
that a version is a moving number nobody re-checks, and one of the pre-registry
deadlines had already expired without anything going red.

⚠ IT IS NOT YET THE ONE PLACE A DEPRECATION IS RECORDED. Two names are registered;
other sites still carry their own prose deadline in FOUR distinct wordings -- the
files carrying them are listed in the registry header, with a warning that the
list is re-checked by reading and not by a one-line grep -- and
**nothing here detects an UNREGISTERED deprecation**: a name that grows a deadline
and no entry is invisible to both gates below, which is why a completeness sweep
is named as the follow-on rather than assumed.

THE TWO GATES RUN IN OPPOSITE DIRECTIONS, and both are needed:

  - BEFORE its milestone, a registered name must still EXIST. An entry naming
    something already gone guards nothing while reading as coverage -- the same
    shape as a test row that cannot fail.
  - AT OR AFTER its milestone, no entry may still be REGISTERED. This is the
    fail-at-beta gate. It fires on the version bump rather than on somebody
    remembering, which is the whole point: the deadline it replaces was a
    sentence in a comment that outlived its own expiry.

WHY THESE LIVE HERE AND NOT IN test_api_dump.py. The registry is part of the api
tooling's generic core (see the .api standalone note), so it sits beside that
file rather than inside gr2's own dump gates -- but it is a separate module
because a deprecation is a property of the SURFACE, and the dump's three gates
are about the dump.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.version import Version as VERSION

HERE = Path(__file__).resolve().parent
GR2 = HERE.parent
REGISTRY = GR2 / "api" / "deprecations.toml"
PYPROJECT = GR2 / "pyproject.toml"

# The version's stages, in ORDER. The first is a PEP 440 dev release
# (`2.0.0.dev1`), which sorts BEFORE every pre-release, so reading it as
# `release` would redden the fail-at-beta gate on a version that is EARLIER than
# alpha -- an over-refusal, and the direction that only feels safe.
STAGE_ORDER = ("dev", "alpha", "beta", "release")

# The CLOSED vocabulary an ENTRY may name. An entry picks one of these or the file
# has no way to tell a milestone from a note. `release` is the end of the line: a
# name marked `release` must be gone by the first non-prerelease version.
#
# `dev` is deliberately NOT in this set: it is a stage the VERSION passes through,
# not a milestone anything is ever removed at, so an entry naming it is a mistake
# this list refuses rather than a fourth word to compare against.
MILESTONES = ("alpha", "beta", "release")


def _current_stage() -> str:
    """The distribution's own stage, derived from its version.

    Read from `pyproject.toml` rather than `importlib.metadata`, because the
    number that matters is the one the distribution DECLARES -- `project.version`
    is authoritative (the runtime reads the same value through
    `importlib.metadata.version("gitgrip")`, which is that field once installed).

    PEP 440 maps onto the stages: a pre-release of `a` -> alpha, `b` -> beta,
    `rc` -> release; a dev release with no pre-release marker -> dev; anything
    else -> release. `rc` counts as REACHED rather than as its own stage: it is
    past beta, so demanding the beta entries be gone is the conservative direction
    and the one that cannot silently pass.

    THE ORDER OF THOSE TWO CHECKS IS LOAD-BEARING, and both cases were measured:
    `2.0.0a5.dev1` is a dev build OF alpha and reads alpha, while `2.0.0.dev1` is
    a dev build of a version with no pre-release marker and sorts BEFORE alpha.
    Reading the second as `release` reddens the fail-at-beta gate on a version
    EARLIER than the one the entries are waiting for -- an over-refusal, which is
    the direction that only feels safe.

    PARSED WITH `packaging.version.Version` RATHER THAN A REGEX, because the
    question is "what stage is this PEP 440 version", and a regex answers a
    narrower one. A regex over the raw string mis-read `2.0.0alpha5` -- a valid,
    non-normalized spelling `Version` normalizes to `2.0.0a5` -- as `release`,
    which is the same over-refusal by a different route. `packaging` is a declared
    test dependency (see the `dev` extra) precisely so this import is honest
    rather than borrowed from pytest.
    """
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    parsed = VERSION(raw)
    if parsed.pre is not None:
        return {"a": "alpha", "b": "beta", "rc": "release"}[parsed.pre[0]]
    if parsed.dev is not None:
        return "dev"
    return "release"


def _registered() -> dict[str, str]:
    data = tomllib.loads(REGISTRY.read_text(encoding="utf-8"))
    entries = data.get("removed-at", {})
    for spelling, milestone in entries.items():
        assert milestone in MILESTONES, (
            f"registry entry {spelling!r} carries {milestone!r}, which is not one of "
            f"{MILESTONES!r}. The vocabulary is closed: an open one cannot be compared "
            f"against a stage, which is the defect this file replaced."
        )
    return entries


def _exists(spelling: str) -> bool:
    """Does the registered name still exist in the tree?

    Two kinds so far, each with the cheapest honest check:

      - `module <name>`: the top-level package directory with an `__init__.py`.
      - `path <p>`: the literal appears at least once under `python_cli/`.

    The `path` check is deliberately loose and says so: it answers "is the literal
    still written anywhere", not "does the product still create it at runtime". A
    tighter check needs a fixture that runs the init path, which belongs with the
    deletion at beta rather than before it.
    """
    kind, _, rest = spelling.partition(" ")
    if kind == "module":
        return (GR2 / rest / "__init__.py").is_file()
    if kind == "path":
        for src in (GR2 / "python_cli").rglob("*.py"):
            if rest in src.read_text(encoding="utf-8", errors="replace"):
                return True
        return False
    raise AssertionError(
        f"registry entry {spelling!r} names kind {kind!r}, which this gate does not know "
        f"how to check. Adding a kind means adding its check HERE, in the same change -- "
        f"an entry nothing can verify is a note, not a registration."
    )


def test_the_registry_parses_and_uses_one_vocabulary() -> None:
    """The floor under both gates: a registry that will not parse gates nothing."""
    entries = _registered()
    assert entries, (
        "the registry is empty. That is a legitimate state -- it is what beta looks like "
        "-- but it is not today's, and an empty file would make both gates below pass "
        "vacuously. If everything here really has been removed, delete the entries and "
        "this assertion together."
    )


def test_a_name_before_its_milestone_still_exists() -> None:
    """Direction one: an entry for something already gone is stale, not coverage."""
    stage = _current_stage()
    stale = [
        spelling
        for spelling, milestone in _registered().items()
        if STAGE_ORDER.index(stage) < STAGE_ORDER.index(milestone) and not _exists(spelling)
    ]
    assert not stale, (
        f"these names are registered as deprecated but no longer exist in the tree: {stale}. "
        f"An entry for a name that is already gone guards nothing while reading as coverage "
        f"-- delete the entry, or the name came back and the entry is now wrong in the other "
        f"direction."
    )


def test_nothing_stays_registered_once_its_milestone_is_reached() -> None:
    """Direction two, and the fail-at-beta gate.

    At or past a name's milestone the entry must be GONE from this file. It fires
    on the version bump: the day `project.version` becomes `2.0.0b1`, every `beta`
    entry turns this red without anyone re-reading a comment.
    """
    stage = _current_stage()
    overdue = [
        f"{spelling} (removed-at {milestone})"
        for spelling, milestone in _registered().items()
        if STAGE_ORDER.index(stage) >= STAGE_ORDER.index(milestone)
    ]
    assert not overdue, (
        f"the distribution has reached {stage!r}, and these names are still registered as "
        f"deprecated: {overdue}. A milestone that arrived is a milestone that must be met: "
        f"remove the name (and its shim, path, or alias) and delete its entry. If the removal "
        f"is genuinely not happening, the entry does not belong in this file -- moving the "
        f"milestone later is the honest edit, and it is a visible one."
    )


# --- THE COMPLETENESS SWEEP: the third gate, and the named follow-on -------------
#
# The registry's own header names this as the missing piece: "the missing piece is a
# sweep that requires every hidden alias in the dump to be either registered or
# explicitly exempted". Without it, a name that grows a prose deadline and no entry is
# invisible to BOTH gates above -- they only ask questions OF THE ENTRIES -- so the
# failure the registry exists to catch is exactly as it was for that name.

DUMP = GR2 / "api" / "cli.api"

# Hidden aliases deliberately NOT registered, each with its reason. An exemption here
# is a DECISION with a sentence; a name that is neither registered nor listed is a
# defect, which is the whole point of the sweep. Empty today is a legitimate state and
# is NOT the same as "nothing to do" -- see the assertion below.
EXEMPT: dict[str, str] = {}


def _hidden_aliases() -> dict[str, str]:
    """Every distinct hidden alias the dump names, with the row it was read from.

    THE DUMP MARKS TWO SHAPES AND BOTH MUST BE READ -- measured on `api/cli.api`:

        verb  review open-gr  (hidden)         <- a bare marker; the alias is the verb
        verb  grip snapshot   (hidden:grip)    <- the marker names the GROUP; the
                                                  hidden thing is `grip` itself, not
                                                  the row's own command

    A pattern matching only the colon form returns ONE alias (`grip`) and misses the
    other five, and a test built on it passes while five aliases stay unregistered --
    a check that cannot fail, which is the defect class this file exists to catch. The
    two shapes are spelled out here so the next reader does not have to rediscover
    which one their regex dropped.
    """
    found: dict[str, str] = {}
    for line in DUMP.read_text(encoding="utf-8").splitlines():
        # NOT anchored at end-of-line: the marker is followed by a padded stability
        # column (`... (hidden)                    stable`). An anchored pattern matches
        # nothing and the sweep then reports an empty dump, which is why the count is
        # asserted below rather than trusted.
        m = re.match(r"^verb\s+(.+?)\s+\(hidden(?::([A-Za-z0-9_-]+))?\)", line)
        if not m:
            continue
        row, group = m.group(1).strip(), m.group(2)
        # `(hidden:<group>)` marks the GROUP as the hidden thing; the bare form marks
        # the row's own last path segment.
        name = group if group else row.split()[-1]
        found.setdefault(name, line.strip())
    return found


def test_every_hidden_alias_is_registered_or_explicitly_exempted() -> None:
    """The completeness sweep: nothing may be hidden AND unaccounted for.

    It fires on the NEXT name that grows a hidden marker, not on a list somebody has
    to remember to update -- which is the difference between a gate and a note.
    """
    aliases = _hidden_aliases()
    assert aliases, (
        f"the sweep found NO hidden aliases in {DUMP.relative_to(GR2)}. Either every "
        f"hidden alias really has been dropped, or the dump changed shape and this "
        f"reader is now matching nothing -- and those two look identical from here, "
        f"which is why the count is asserted rather than assumed."
    )
    registered = {spelling.partition(" ")[2] for spelling in _registered()}
    unaccounted = sorted(n for n in aliases if n not in registered and n not in EXEMPT)
    assert not unaccounted, (
        "these names are hidden but neither registered in api/deprecations.toml nor "
        "explicitly exempted:\n  "
        + "\n  ".join(f"{n}   (from: {aliases[n]})" for n in unaccounted)
        + "\n\nRegister each with a milestone, or add it to EXEMPT with a sentence "
        "saying why it is not a deprecation. A hidden name with no entry and no reason "
        "is invisible to both registry gates."
    )
