#!/usr/bin/env python3
"""Generate ``api/cli.api`` -- the committed dump of gr2's command-line surface.

The file is generated, sorted, one item per line, and carries a stability
marker. A change to the public surface must change this file in the same PR;
the gate that enforces that is ``tests/test_api_dump.py``.

Item kinds emitted here: ``verb``, ``flag``, ``arg``, ``exit``, ``json``,
``path`` and ``ref`` -- each from the table that can honestly answer for it,
never from the app walk alone.

``json`` comes from ``JSON_SHAPES``, the table beside the store renderers that
emit the payloads (``gr2/python_cli/grip_cli.py``); a verb carrying ``--json``
with no entry there is a shape the dump does not promise, and the gate that
holds that line is ``test_the_store_group_json_verbs_all_have_a_shape``.

``path`` and ``ref`` come from ``LAYOUT`` in ``gr2/python_cli/layout.py``, the
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

HERE = Path(__file__).resolve().parent
API_DIR = HERE.parent / "api"
CLI_API = API_DIR / "cli.api"
STABILITY = API_DIR / "stability.toml"

# The spelling column is padded to this width so the stability markers line up
# and a diff reads as a change to one item rather than to the whole block.
COLUMN = 64


def _walk(cmd, prefix: tuple[str, ...] = (), hidden_by: str | None = None):
    """Every leaf command, as (path, command, hidden_by).

    Hidden is INHERITED: a command is hidden when its own flag says so OR any
    ancestor group is hidden. Typer marks a sub-app hidden at the GROUP level
    (``add_typer(app, name="grip", hidden=True)``), and the group's children
    keep ``hidden=False`` of their own -- so reading only the leaf reports a
    hidden group's whole subtree as part of the visible surface. Measured: the
    ``grip`` alias group is hidden and 28 of its items were dumped as visible.

    ``hidden_by`` NAMES the ancestor group that did the hiding, because the dump
    carries no group row of its own: without the name, one group flag reads as
    dozens of identical lines and the cause is invisible to a reader.
    """
    subs = getattr(cmd, "commands", None)
    if subs:
        if getattr(cmd, "hidden", False) and prefix:
            hidden_by = prefix[-1]
        out: list[tuple[tuple[str, ...], object, str | None]] = []
        for name, sub in subs.items():
            out.extend(_walk(sub, prefix + (name,), hidden_by))
        return out
    return [(prefix, cmd, hidden_by)]


def _items() -> list[tuple[str, str, bool, str | None]]:
    """(kind, spelling, hidden, hidden_by) for every surface item, unsorted."""
    from gr2.python_cli.app import app

    top = typer.main.get_command(app)
    rows: list[tuple[str, str, bool, str | None]] = []
    for path, cmd, hidden_by in _walk(top):
        verb = " ".join(path)
        # Hidden is the leaf's own flag OR an ancestor group's, which `_walk`
        # reports as the name of the group that did it.
        hidden = bool(getattr(cmd, "hidden", False)) or hidden_by is not None
        rows.append(("verb", verb, hidden, hidden_by))
        for prm in getattr(cmd, "params", []):
            opts = list(getattr(prm, "opts", None) or [])
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
            rows.append(("flag" if is_option else "arg", spelling, hidden, hidden_by))
    rows.extend(_exit_items())
    rows.extend(_json_items())
    rows.extend(_layout_items())
    return rows


def _json_items() -> list[tuple[str, str, bool, str | None]]:
    """The `json` kind, from the ``JSON_SHAPES`` table beside the renderers.

    THE TABLE IS THE ONLY SOURCE, and it lives with the code that emits the
    payload (``gr2/python_cli/grip_cli.py``), so a reviewer reads the shape and
    the renderer in one place. Nothing here re-derives a key from the app walk:
    a Typer ``--json`` flag says a verb HAS a payload, never what is in it, and
    a dump that guessed would be a promise nobody made.

    Hidden is always False, for the reason ``_exit_items`` gives about exit
    codes: the item is in the dump because it is part of the group's published
    contract, and the verb's own visibility is already carried by its own row
    (``store snapshot`` is visible as hidden on its ``verb`` row).

    A KEY CANNOT BE MORE PROMISED THAN ITS VERB, so the marker is INHERITED
    rather than looked up here -- see ``_marker``. While the store group is
    may-change, so are the keys it emits, and they travel together when that
    marker is lifted.
    """
    from gr2.python_cli import grip_cli

    rows: list[tuple[str, str, bool, str | None]] = []
    for verb, paths in grip_cli.JSON_SHAPES.items():
        for path in paths:
            rows.append(("json", f"{verb} {path}", False, None))
    return rows


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


def _layout_items() -> list[tuple[str, str, bool, str | None]]:
    """The `path` and `ref` kinds, from the LAYOUT table (design section 3).

    Hidden is always False here, for the reason `_exit_items` gives about exit
    codes: the row is in the dump because section 3 promises it, and it is not
    any one verb's surface. A `ref` row marked `reserved` STAYS in the dump --
    which is the opposite of what `reserved` means in `api/stability.toml`,
    where an entry asserts the spelling is ABSENT. The difference is deliberate
    and is stated in the layout module: a reader of the layout must be able to
    see that the namespace exists and that nothing writes it yet.
    """
    return [(kind, label, False, None) for kind, label, _ in _layout_rows()]


def _marker(kind: str, spelling: str, markers: dict[tuple[str, str], str]) -> str:
    """The marker for one item, from whichever table owns its kind.

    THREE SOURCES, each for the kind it can honestly answer for. A `path` or
    `ref` row's promise is a property OF THE LAYOUT (section 3's own stability
    column), not a decision taken in review, so those two kinds read the layout
    table. A `json` item INHERITS its canonical verb's marker: inheritance is a
    rule rather than a lookup, and an explicit entry for a json spelling is
    deliberately not consulted, because the only honest source for "may this key
    change" is the verb's own promise. Everything else is the plain lookup with
    the default the module docstring states -- stable unless named.

    The layout table and `api/stability.toml` are kept DISJOINT. A spelling
    carrying a marker from both would be answered twice and the winner would
    depend on lookup order rather than on anyone's decision, and
    `test_no_spelling_carries_a_marker_from_both_sources` asserts it.
    """
    if kind in ("path", "ref"):
        return {  # keyed by the same label the item carries
            (row_kind, label): marker for row_kind, label, marker in _layout_rows()
        }.get((kind, spelling), "stable")
    if kind == "json":
        return markers.get(("verb", _canonical_verb(_verb_of_json(spelling))), "stable")
    return markers.get((kind, spelling), "stable")


def _exit_items() -> list[tuple[str, str, bool, str | None]]:
    """The `exit` kind, from the group tables rather than from the app walk.

    Exit codes are not discoverable by walking a Typer app -- they are chosen in
    each verb's error path -- so the table is the only honest source, and having
    one makes the contract diffable. Hidden is always False here: an exit code is
    either part of the group's published contract or it is not in the table.

    THE PREFIX IS READ FROM ITS OWN HOME, `grip_cli.STORE_INCOMPLETE_PREFIX`,
    rather than copied, so there is one string and the dump cannot drift from
    the message a caller actually matches on.
    """
    from gr2.python_cli import grip_cli
    from gr2.python_cli.exit_codes import EXIT_CODES, PREFIXED_CODES

    rows: list[tuple[str, str, bool, str | None]] = []
    for group, codes in EXIT_CODES.items():
        for code, reason in sorted(codes.items()):
            # Code 5 carries two shapes at one code: a MEASURED cannot-measure
            # and a wrapped exception. The prefix is the only thing separating
            # them, so it rides the reason field -- it is the reason this code
            # has two meanings, which is exactly what the reason field is for.
            spelling = f"{group} {code} {reason}"
            if (group, code) in PREFIXED_CODES:
                spelling += f' prefix:"{grip_cli.STORE_INCOMPLETE_PREFIX}"'
            rows.append(("exit", spelling, False, None))
    return rows


def _markers() -> dict[tuple[str, str], str]:
    """(kind, spelling) -> 'may-change' | 'reserved', from api/stability.toml."""
    if not STABILITY.exists():
        return {}
    import tomllib

    data = tomllib.loads(STABILITY.read_text())
    out: dict[tuple[str, str], str] = {}
    for marker in ("may-change", "reserved"):
        for entry in data.get(marker, []):
            kind, _, spelling = entry.partition(" ")
            out[(kind, spelling)] = marker
    return out


def _line(kind: str, label: str, marker: str) -> str:
    """One ``.api`` line: two fixed columns, with the separator GUARANTEED.

    A label at or past ``COLUMN`` is emitted at its own length, so padding it to
    ``COLUMN`` adds nothing and the marker runs straight into the label. Every
    label in this dump contains spaces (a flag reads ``verb --flag <str>``), so a
    reader splitting the two columns then takes a space INSIDE the label as the
    separator and recovers neither column.

    Measured before this rule existed: the 65-char ``exit`` label rendered as
    ``...store verb: "may-change``, whose last space is the one inside the quoted
    prefix. The columns came back as the label truncated at ``verb:`` and a marker
    of ``"may-change``, the marker lookup then missed, and every exit line
    silently read as ``stable``.

    Pulled out of ``render`` so the width rule has ONE home and a synthetic long
    label can be tested against it directly, rather than only through whichever
    labels happen to reach ``COLUMN`` today.
    """
    width = max(COLUMN, len(label) + 1)
    return f"{kind:<6}{label:<{width}}{marker}"


def render() -> str:
    """The exact bytes of api/cli.api, terminated by one newline."""
    markers = _markers()
    lines = []
    for kind, spelling, hidden, hidden_by in _items():
        marker = _marker(kind, spelling, markers)
        # An inherited hide names the group that did it; a command hidden in
        # its own right is just `(hidden)`. The dump carries no group row, so
        # the name is the only place the cause can live.
        if hidden:
            label = f"{spelling} (hidden:{hidden_by})" if hidden_by else f"{spelling} (hidden)"
        else:
            label = spelling
        lines.append(_line(kind, label, marker))
    lines.sort()
    return "\n".join(lines) + "\n"


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
