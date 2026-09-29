#!/usr/bin/env python3
"""Generate ``api/cli.api`` -- the committed dump of gr2's command-line surface.

The file is generated, sorted, one item per line, and carries a stability
marker. A change to the public surface must change this file in the same PR;
the gate that enforces that is ``tests/test_api_dump.py``.

Item kinds emitted here: ``verb``, ``flag``, ``arg`` and ``exit``. A fuller dump
also carries ``json``, ``path`` and ``ref`` items; those need the ``JSON_SHAPES``
/ ``LAYOUT`` tables and append here when they exist rather than replacing
anything.

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
    return rows


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
        marker = markers.get((kind, spelling), "stable")
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
