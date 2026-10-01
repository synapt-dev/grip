"""The generic core of the API dumper: walker, line format, marker lookup.

THE SEAM. This module knows three things -- how to walk a command tree, how an
``.api`` line is shaped, and how a stability table is keyed -- and it knows
nothing about gr2: not its app, not its tables, not its exit codes. Everything
that names gr2 lives in the consumer (``gr2/scripts/dump_api.py``), which
REGISTERS the kinds it emits and supplies the marker rules for them.

The boundary is a test, not a convention: ``tests/test_api_core_seam.py`` walks
this module's imports at any depth and fails if any of them reaches gr2, with
the plugin beside it as the control that proves the walker can fail.

WHY IT IS WORTH THE SPLIT. The dumper does not move to its own repo yet -- but
the regroup of the dump by command and subcommand is the moment to cut the
seam, so that the later extraction is a MOVE rather than a rewrite. A core with
no gr2 import can be lifted out with its tests; one that reaches into
``grip_cli`` for an exit prefix cannot.

WHAT IS NOT HERE YET, named rather than implied: the alias registry
(``grip_cli.STORE_MOUNTS`` is gr2's own mount table) and the three gates are
still on the plugin side, because both answer questions about gr2's surface
rather than about the format. They move when a second consumer exists to make
their generic shape decidable.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# (kind, spelling, hidden, hidden_by) -- one item, before it is rendered.
Row = tuple[str, str, bool, str | None]

# (kind, spelling) -> "may-change" | "reserved". An item absent here is stable.
Markers = dict[tuple[str, str], str]

# The spelling column is padded to this width so the stability markers line up
# and a diff reads as a change to one item rather than to the whole block.
COLUMN = 64


def walk(cmd, prefix: tuple[str, ...] = (), hidden_by: str | None = None):
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
            out.extend(walk(sub, prefix + (name,), hidden_by))
        return out
    return [(prefix, cmd, hidden_by)]


def label(spelling: str, hidden: bool, hidden_by: str | None) -> str:
    """The label column for one item -- the text BEFORE any parsing strips it.

    One home for the rule, because a gate that wants to ask a question about the
    label TEXT (does it end in a space? how long is it?) cannot use the parsed
    rows: a parser strips the padding, and the trailing space it would be asking
    about is exactly what stripping removes. Measured -- a first version of
    ``test_no_label_ends_in_a_space`` read labels through a parser and could not
    have failed for any input, because every label it saw had already been
    stripped.

    An inherited hide names the group that did it; a command hidden in its own
    right is just ``(hidden)``. The dump carries no group row, so the name is the
    only place the cause can live.
    """
    if not hidden:
        return spelling
    return f"{spelling} (hidden:{hidden_by})" if hidden_by else f"{spelling} (hidden)"


def line(kind: str, label: str, marker: str) -> str:
    """One ``.api`` line: two fixed columns, with the separator GUARANTEED.

    A label at or past ``COLUMN`` is emitted at its own length, so padding it to
    ``COLUMN`` adds nothing and the marker runs straight into the label. Every
    label in this dump contains spaces (a flag reads ``verb --flag <str>``), so a
    reader splitting the two columns then takes a space INSIDE the label as the
    separator and recovers neither column.

    Measured before this rule existed: the 65-char ``exit`` label rendered as
    ``...store verb: "may-change``, whose last space is the one inside the quoted
    prefix. The columns came back as the label truncated at ``verb:`` and a
    marker of ``"may-change``, the marker lookup then missed, and every exit line
    silently read as ``stable``.

    Pulled out of ``render`` so the width rule has ONE home and a synthetic long
    label can be tested against it directly, rather than only through whichever
    labels happen to reach ``COLUMN`` today.

    ONE SHAPE THIS CANNOT FIX, because the ambiguity is on the reader's side of
    the line: a label whose LAST character is a space is indistinguishable from
    the padding, so a reader stripping the padding returns it one character
    short. Guaranteeing the separator does not help -- the space is inside the
    label, before it. No label ends in a space today, and
    ``test_no_label_ends_in_a_space`` keeps it that way.
    """
    width = max(COLUMN, len(label) + 1)
    return f"{kind:<6}{label:<{width}}{marker}"


def load_markers(path: Path) -> Markers:
    """(kind, spelling) -> 'may-change' | 'reserved', from a stability table.

    An absent file is an empty table rather than an error: a checkout with no
    stability table has made no promises, and every item reading ``stable``
    there is the stated default. The entries are ``"<kind> <spelling>"``, split
    on the FIRST space, because a spelling carries spaces of its own.
    """
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    out: Markers = {}
    for marker in ("may-change", "reserved"):
        for entry in data.get(marker, []):
            kind, _, spelling = entry.partition(" ")
            out[(kind, spelling)] = marker
    return out


def plain_marker(kind: str) -> Callable[[str, Markers], str]:
    """The default marker rule for a kind: plain lookup, stable unless named.

    Defaulting to stable is deliberate -- a new item is stable unless somebody
    says otherwise in review -- and it is the rule most kinds want, so a
    consumer registering one writes ``plain_marker("exit")`` rather than a
    four-line closure that has to be read to be trusted.
    """

    def resolve(spelling: str, markers: Markers) -> str:
        return markers.get((kind, spelling), "stable")

    return resolve


@dataclass(frozen=True)
class Kind:
    """One item kind, as the consumer registers it.

    ``items`` yields ``Row``s; ``marker`` answers for this kind alone, which is
    the point of registering per kind -- the lookup rule for a ``json`` key path
    (inherit the canonical verb's promise) is not the rule for an ``exit`` code
    (plain lookup) and a single dispatching function had to know both.
    """

    name: str
    items: Callable[[], list[Row]]
    marker: Callable[[str, Markers], str]


class Registry:
    """The kinds this dump carries, in the order the consumer registered them."""

    def __init__(self) -> None:
        self._kinds: list[Kind] = []

    def register(
        self,
        name: str,
        items: Callable[[], list[Row]],
        marker: Callable[[str, Markers], str],
    ) -> Kind:
        if any(kind.name == name for kind in self._kinds):
            raise ValueError(f"kind {name!r} is already registered")
        kind = Kind(name=name, items=items, marker=marker)
        self._kinds.append(kind)
        return kind

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(kind.name for kind in self._kinds)

    def rows(self) -> list[Row]:
        out: list[Row] = []
        for kind in self._kinds:
            out.extend(kind.items())
        return out

    def marker(self, kind: str, spelling: str, markers: Markers) -> str:
        """The marker for one item, from the kind that owns it.

        An UNREGISTERED kind is refused rather than defaulted. Answering
        ``stable`` for a kind nobody registered would publish a promise no one
        made, and it would do it silently -- the failure would look exactly like
        an item the reviewers simply never named.
        """
        for registered in self._kinds:
            if registered.name == kind:
                return registered.marker(spelling, markers)
        raise KeyError(
            f"no kind {kind!r} is registered; registered: {list(self.names)}"
        )


def render(registry: Registry, markers: Markers | None = None) -> str:
    """The exact bytes of an ``.api`` file, terminated by one newline."""
    markers = {} if markers is None else markers
    lines = [
        line(kind, label(spelling, hidden, hidden_by), registry.marker(kind, spelling, markers))
        for kind, spelling, hidden, hidden_by in registry.rows()
    ]
    lines.sort()
    return "\n".join(lines) + "\n"
