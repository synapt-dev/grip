"""The layout table: one home for gr2's paths and ref namespaces.

Design section 3 fixes the layout, and section 7 says it becomes ``layout.api``.
This module is where those rows live, and where a later change makes it the ONLY
place a gr2 path constant is defined (that refactor is its own lane, not this
one).

WHAT IS HERE TODAY IS THE PART THE DESIGN FIXES EXACTLY. Section 7's worked
example carries ``path grip.toml tracked`` and the two reserved ref namespaces,
and those are emitted into ``cli.api`` beside the verb, flag, arg, exit and json
kinds. THE REST OF SECTION 3'S TABLE IS DELIBERATELY NOT HERE YET -- the
``.grip`` state tree, the review-record coordinate, the overlay family, the
alpha-store paths. Those rows are layout decisions still open on another desk,
and a dump freezes what it carries, so writing them here would freeze decisions
nobody has made. They land with ``layout.api``, in that order, which is the
order the lead set.

A ROW'S MARKER COMES FROM THE ROW, not from ``api/stability.toml``. The reason
is that the promise is a property OF THE LAYOUT rather than a decision taken in
review: a namespace is reserved because beta will write it, not because somebody
marked it tonight. The two sources are therefore kept disjoint, and the gate
asserts it -- a spelling carrying a marker from both would be answered twice,
and which answer wins would depend on lookup order.

AND ``reserved`` MEANS SOMETHING DIFFERENT IN EACH SOURCE, which is why they are
kept apart rather than merged. In ``stability.toml`` it means the name is HELD
AND ABSENT from the dump (the gate reddens the day it appears). Here it means the
row is PRESENT AND MARKED, because a reader of the layout must be able to see
that the namespace exists and that nothing writes it yet. A path row would never
carry it; the reserved ref namespaces do.
"""

from __future__ import annotations

import dataclasses

#: The markers a layout row may carry. Asserted, so a typo cannot become a
#: fourth marker that the share gate then reads as "not reserved".
MARKERS = ("stable", "may-change", "reserved")


@dataclasses.dataclass(frozen=True)
class LayoutRow:
    """One row of the section 3 table.

    ``tracked`` is that table's "Tracked in the root?" column, kept because it
    is the half of a path row a caller cannot derive: whether the root repo
    carries the path in its history.
    """

    kind: str  # "path" | "ref"
    spelling: str
    tracked: str = ""
    marker: str = "stable"
    note: str = ""


LAYOUT: tuple[LayoutRow, ...] = (
    LayoutRow(
        kind="path",
        spelling="grip.toml",
        tracked="tracked",
        note=(
            "the workspace spec, the canonical source of every pin (section 3). "
            "Root-relative: section 3 writes it `<root>/grip.toml`, and the dump "
            "carries the path as a member of the workspace rather than as an "
            "absolute location on one machine."
        ),
    ),
    LayoutRow(
        kind="ref",
        spelling="refs/dev.synapt/__members__/<member>/heads/*",
        marker="reserved",
        note=(
            "mirrors, beta, not written in the slice. The namespace is reserved "
            "rather than merely unused: every ref synapt writes lives under "
            "`refs/dev.synapt/`, a word WE fixed is written `__x__`, and a "
            "user-chosen name is bare, so a user name can only ever sit as a leaf "
            "under a reserved directory, and a directory/file ref conflict cannot "
            "arise when a new kind is added."
        ),
    ),
    LayoutRow(
        kind="ref",
        spelling="refs/dev.synapt/__overlays__/<member>/...",
        marker="reserved",
        note="overlays, beta, not written in the slice (section 3).",
    ),
)
