#!/usr/bin/env python3
"""Generate ``api/cli.api`` -- the committed dump of gr2's command-line surface.

The file is generated, GROUPED BY COMMAND, one item per line, and carries a
stability marker. A change to the public surface must change this file in the
same PR; the gate that enforces that is ``tests/test_api_dump.py``.

Grouping is a core rule (``gr2/api_core.py``) over a fact only this file can
supply: which command each item belongs to. The walk hands it over for free for
verbs, flags and positionals; the json table supplies its own verb key; an exit
code is filed under the group that returns it; and the layout rows, which belong
to no verb at all, render under ``LAYOUT_GROUP``.

Item kinds emitted here: ``verb``, ``flag``, ``arg``, ``exit``, ``json``,
``path`` and ``ref`` -- each from the table that can honestly answer for it,
never from the app walk alone.

THIS FILE IS THE PLUGIN HALF OF A SEAM. Everything generic -- the walker, the
line format, the marker table, the registry -- is in ``gr2/api_core.py``, which
imports nothing from gr2 and is where the later extraction will start. What
lives here is what names gr2: its app, its tables, its alias mounts. The
boundary is held by ``tests/test_api_core_seam.py``, not by this paragraph.

``json`` comes from ``JSON_SHAPES``, the table beside the store renderers that
emit the payloads (``gr2/gr2/python_cli/grip_cli.py``); a verb carrying ``--json``
with no entry there is a shape the dump does not promise, and the gate that
holds that line is ``test_the_store_group_json_verbs_all_have_a_shape``.

``path`` and ``ref`` come from ``LAYOUT`` in ``gr2/gr2/python_cli/layout.py``, the
design's section 3 table. That table is the honest source for the same reason
the exit table is: a path is not discoverable by walking a Typer app -- nothing
in the command tree says which of the strings the code builds is part of the
promised layout. Section 3 carries only the rows it fixes exactly so far; its
remaining rows land with ``layout.api``, and the module docstring says why
writing them here early would freeze decisions nobody has made.

Stability comes from ``api/stability.toml``: an item named there as
``may-change`` or ``reserved`` carries that marker, and every other item is
``stable``. Defaulting to stable is deliberate: a new item is stable unless
somebody says otherwise in review.

Usage:
    python scripts/dump_api.py            # write api/cli.api
    python scripts/dump_api.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import typer

from gr2 import api_core
from gr2.api_core import Registry

HERE = Path(__file__).resolve().parent
API_DIR = HERE.parent / "api"
CLI_API = API_DIR / "cli.api"
STABILITY = API_DIR / "stability.toml"

# Re-exported from the core so there is ONE implementation and the gates can
# still reach them here. `test_the_plugin_uses_the_core_rather_than_a_copy_of_it`
# asserts these are the core's own objects rather than equal-looking copies.
COLUMN = api_core.COLUMN
_walk = api_core.walk
_label = api_core.label
_line = api_core.line


def _verb_of_json(spelling: str) -> str:
    """The verb path a ``json`` spelling belongs to.

    A verb path CONTAINS SPACES (``store status``) and a key path may carry an
    annotation after two spaces, so neither the first nor the last space is the
    boundary. The split is on the first ``" ."`` -- the dot that starts a key
    path -- which is the only one of the three that is unambiguous.

    ``test_every_json_item_names_a_real_verb`` pins the other half: whatever
    comes back has to name a verb the walk actually produced, so a table entry
    with a typo cannot silently inherit the default marker.
    """
    verb, separator, _ = spelling.partition(" .")
    return verb if separator else spelling


def _canonical_verb(verb: str) -> str:
    """A verb path with an ALIAS MOUNT resolved to the canonical one.

    ``grip status`` and ``store status`` are the same callbacks behind two
    mounts (``grip_cli.STORE_MOUNTS``), so they are the same verb and must carry
    the same marker. ``api/stability.toml`` names only the canonical mount, so an
    alias spelling looks up as if absent and silently takes the ``stable``
    default -- which is a STRONGER promise about the identical payload than the
    one its twin publishes. Measured before this rule: 51 ``grip`` key paths read
    ``stable`` against 51 ``store`` twins reading ``may-change``.

    The mount pair is read from the tuple ``JSON_SHAPES`` expands over rather
    than retyped, so the two cannot disagree about which mount is canonical or
    how many there are.

    ONLY THE ``json`` KIND RESOLVES. The alias's ``verb``/``flag``/``arg`` rows
    carry the same shape of inconsistency and are deliberately left for their own
    change: they are hidden and leave the share denominator, so they move no
    gate, while the ``json`` rows are what this range newly publishes.
    """
    from gr2.python_cli import grip_cli

    mounts = grip_cli.STORE_MOUNTS
    mount, separator, rest = verb.partition(" ")
    if separator and mount in mounts[1:]:
        return f"{mounts[0]} {rest}"
    return verb


# The block the LAYOUT rows render under. They belong to no verb -- a path and
# a ref are actionable on their own -- so they need a heading that is visibly
# NOT a command, and the parentheses also sort the block ahead of every real
# command rather than dropping it in the middle of the alphabet between two of
# them, where a reader would take it for one.
LAYOUT_GROUP = "(layout)"


def _layout_rows() -> list[tuple[str, str, str]]:
    """(kind, label, marker) for every LAYOUT row.

    ONE CONSTRUCTION, used by the item emitter and by the marker lookup, so the
    label an item carries and the spelling its marker is keyed by cannot drift
    apart. A path row's label carries the tracked column ("grip.toml tracked")
    because a caller cannot derive whether the root repo holds the path in its
    history; a ref row is the namespace alone.
    """
    from gr2.python_cli.layout import LAYOUT

    return [
        (
            row.kind,
            f"{row.spelling} {row.tracked}" if row.tracked else row.spelling,
            row.marker,
        )
        for row in LAYOUT
    ]


def _walked_items() -> list[api_core.Row]:
    """The app walk, run ONCE, as (kind, spelling, hidden, hidden_by) rows.

    One walk rather than one per kind: ``verb``, ``flag`` and ``arg`` all come
    out of the same traversal, and three registrations each calling this would
    walk the whole command tree three times for the same answer.
    """
    from gr2.python_cli.app import app

    top = typer.main.get_command(app)
    rows: list[api_core.Row] = []
    for path, cmd, hidden_by in api_core.walk(top):
        verb = " ".join(path)
        # Hidden is the leaf's own flag OR an ancestor group's, which the walk
        # reports as the name of the group that did it.
        hidden = bool(getattr(cmd, "hidden", False)) or hidden_by is not None
        # THE GROUP IS THE COMMAND PATH, and this is the only place the tree is
        # in hand -- so nothing downstream has to re-derive a command by
        # splitting a spelling, and an item cannot be filed under a command the
        # walk never produced.
        rows.append(api_core.Row("verb", verb, hidden, hidden_by, verb))
        for prm in getattr(cmd, "params", []):
            opts = list(getattr(prm, "opts", None) or [])
            opts.extend(getattr(prm, "secondary_opts", None) or [])
            if not opts:
                continue
            # An OPTION's first spelling starts with a dash; a POSITIONAL
            # parameter's "opt" is just its name. Both arrive here with a
            # truthy `opts`, so the kind is decided by the spelling, not by
            # the attribute being present.
            is_option = any(opt.startswith("-") for opt in opts)
            takes_value = not getattr(prm, "is_flag", False)
            spelling = f"{verb} {'/'.join(opts)}"
            if takes_value:
                spelling += " <str>"
            rows.append(
                api_core.Row(
                    "flag" if is_option else "arg", spelling, hidden, hidden_by, verb
                )
            )
    return rows


def _json_items() -> list[api_core.Row]:
    """The `json` kind, from the ``JSON_SHAPES`` table beside the renderers.

    THE TABLE IS THE ONLY SOURCE, and it lives with the code that emits the
    payload (``gr2/gr2/python_cli/grip_cli.py``), so a reviewer reads the shape and
    the renderer in one place. Nothing here re-derives a key from the app walk:
    a Typer ``--json`` flag says a verb HAS a payload, never what is in it, and
    a dump that guessed would be a promise nobody made.

    Hidden is always False, for the reason ``_exit_items`` gives about exit
    codes: the item is in the dump because it is part of the group's published
    contract, and the verb's own visibility is already carried by its own row
    (``store snapshot`` is visible as hidden on its ``verb`` row).

    A KEY CANNOT BE MORE PROMISED THAN ITS VERB, so the marker is INHERITED
    rather than looked up here -- see ``_json_marker``. While the store group is
    may-change, so are the keys it emits, and they travel together when that
    marker is lifted.
    """
    from gr2.python_cli import grip_cli

    rows: list[api_core.Row] = []
    for verb, paths in grip_cli.JSON_SHAPES.items():
        for path in paths:
            # The verb the TABLE keys by IS the command the key belongs to, so
            # the group is read off the table rather than re-derived by
            # splitting the spelling -- one source, no second parse.
            rows.append(api_core.Row("json", f"{verb} {path}", False, None, verb))
    return rows


def _layout_items() -> list[api_core.Row]:
    """The `path` and `ref` kinds, from the LAYOUT table (design section 3).

    Hidden is always False here, for the reason `_exit_items` gives about exit
    codes: the row is in the dump because section 3 promises it, and it is not
    any one verb's surface. A `ref` row marked `reserved` STAYS in the dump --
    which is the opposite of what `reserved` means in ``api/stability.toml``,
    where an entry asserts the spelling is ABSENT. The difference is deliberate
    and is stated in the layout module: a reader of the layout must be able to
    see that the namespace exists and that nothing writes it yet.
    """
    return [
        api_core.Row(kind, label, False, None, LAYOUT_GROUP)
        for kind, label, _ in _layout_rows()
    ]


def _exit_items() -> list[api_core.Row]:
    """The `exit` kind, from the group tables rather than from the app walk.

    Exit codes are not discoverable by walking a Typer app -- they are chosen in
    each verb's error path -- so the table is the only honest source, and having
    one makes the contract diffable. Hidden is always False here: an exit code is
    either part of the group's published contract or it is not in the table.

    THE PREFIX IS READ FROM ITS OWN HOME, ``grip_cli.STORE_INCOMPLETE_PREFIX``,
    rather than copied, so there is one string and the dump cannot drift from
    the message a caller actually matches on.
    """
    from gr2.python_cli import grip_cli
    from gr2.python_cli.exit_codes import EXIT_CODES, PREFIXED_CODES

    rows: list[api_core.Row] = []
    for group, codes in EXIT_CODES.items():
        for code, reason in sorted(codes.items()):
            # Code 5 carries two shapes at one code: a MEASURED cannot-measure
            # and a wrapped exception. The prefix is the only thing separating
            # them, so it rides the reason field -- it is the reason this code
            # has two meanings, which is exactly what the reason field is for.
            spelling = f"{group} {code} {reason}"
            if (group, code) in PREFIXED_CODES:
                spelling += f' prefix:"{grip_cli.STORE_INCOMPLETE_PREFIX}"'
            # An exit code belongs to the GROUP that returns it rather than to
            # any one verb inside the group: the table is keyed by group and the
            # code is shared by everything under it.
            rows.append(api_core.Row("exit", spelling, False, None, group))
    return rows


def _layout_marker(kind: str):
    """The marker rule for a ``path`` or ``ref`` row.

    THESE TWO KINDS READ THE LAYOUT TABLE, not ``api/stability.toml``: their
    promise is a property OF THE LAYOUT (section 3's own stability column), not
    a decision taken in review. The rule is bound per kind because a resolver
    registered for ``path`` must not answer for ``ref``.

    The layout table and ``api/stability.toml`` are kept DISJOINT. A spelling
    carrying a marker from both would be answered twice and the winner would
    depend on lookup order rather than on anyone's decision, and
    ``test_no_spelling_carries_a_marker_from_both_sources`` asserts it.
    """

    def resolve(spelling: str, markers: api_core.Markers) -> str:
        return {  # keyed by the same label the item carries
            (row_kind, row_label): row_marker
            for row_kind, row_label, row_marker in _layout_rows()
        }.get((kind, spelling), "stable")

    return resolve


def _json_marker(spelling: str, markers: api_core.Markers) -> str:
    """A ``json`` item INHERITS its canonical verb's marker.

    Inheritance is a rule rather than a lookup, and an explicit entry for a json
    spelling is deliberately not consulted, because the only honest source for
    "may this key change" is the verb's own promise. Anything not named is
    ``stable``, which is the module-level default and not a judgement here.
    """
    return markers.get(("verb", _canonical_verb(_verb_of_json(spelling))), "stable")


def _subset(rows: list[api_core.Row], kind: str):
    """The rows of one kind out of a table that emitted several."""
    return lambda: [row for row in rows if row[0] == kind]


def _registry() -> Registry:
    """The kinds this dump carries, each with the table and rule that own it.

    THREE SOURCES, each for the kind it can honestly answer for:

    * ``verb``/``flag``/``arg`` from the app walk, plain lookups in
      ``api/stability.toml``.
    * ``path``/``ref`` from the LAYOUT table for BOTH their items and their
      markers, because section 3's stability column is their own promise.
    * ``json`` items from ``JSON_SHAPES`` but markers inherited from the
      canonical verb, because a key cannot be more promised than its verb.
    * ``exit`` from the group tables, plain lookups.

    EVERYTHING ELSE IS THE PLAIN LOOKUP with the default the module docstring
    states -- stable unless named.
    """
    registry = Registry()

    walked = _walked_items()
    for kind in ("verb", "flag", "arg"):
        registry.register(kind, _subset(walked, kind), api_core.plain_marker(kind))

    registry.register("exit", _exit_items, api_core.plain_marker("exit"))
    registry.register("json", _json_items, _json_marker)

    # One LAYOUT read, two registrations: `path` and `ref` come out of the same
    # table but must not answer a marker for each other.
    layout = _layout_items()
    for kind in ("path", "ref"):
        registry.register(kind, _subset(layout, kind), _layout_marker(kind))

    return registry


def _items() -> list[api_core.Row]:
    """(kind, spelling, hidden, hidden_by) for every surface item, unsorted."""
    return _registry().rows()


def _markers() -> api_core.Markers:
    """(kind, spelling) -> 'may-change' | 'reserved', from api/stability.toml."""
    return api_core.load_markers(STABILITY)


def render() -> str:
    """The exact bytes of api/cli.api, terminated by one newline."""
    return api_core.render(_registry(), _markers())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed api/cli.api differs",
    )
    args = parser.parse_args(argv)

    rendered = render()
    if args.check:
        current = CLI_API.read_text() if CLI_API.exists() else ""
        if current != rendered:
            print(f"{CLI_API} is not current: regenerate and name the change in the PR body")
            return 1
        print(f"{CLI_API} is current")
        return 0

    API_DIR.mkdir(parents=True, exist_ok=True)
    CLI_API.write_text(rendered)
    total = len(rendered.splitlines())
    print(f"wrote {CLI_API} ({total} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
