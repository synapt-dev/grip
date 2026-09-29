"""Exit codes, per command group, as a published surface.

§7 of the thin-slice design: exit codes come from **one `EXIT_CODES` table per
group**, and every entry goes into ``api/cli.api`` as an ``exit`` line. The
table lives here rather than beside the verbs so the dump has one import and a
group's exit contract has one home — a table that exists only as prose in the
group's help text cannot be diffed, and the currency gate has nothing to hold it
to.

WHY A CODE CAN APPEAR TWICE. ``store`` returns 5 for two different shapes: a
MEASURED cannot-measure, and a verb that could not COMPLETE because of an
unexpected failure. §5's table has no code of its own for an internal error, so
the internal one reports 5 and prefixes its message — and
``PREFIXED_CODES`` names those, because the prefix is the only thing a caller
can match on to tell the two apart. That is not a style choice; it is the reason
the code has two meanings at all.
"""

from __future__ import annotations

# group -> {code: reason}. The reason is the slug that travels in the `.api`
# line, so a stranger reads §5's table out of the dump rather than out of a
# docstring: `exit   store 3 refused-coverage   stable`.
EXIT_CODES: dict[str, dict[int, str]] = {
    "store": {
        0: "ok",
        2: "usage",
        3: "refused-coverage",
        4: "refused-inconsistent",
        5: "cannot-measure",
    },
}

# The (group, code) pairs whose message carries a prefix, and the name of the
# constant holding it. Named as a set rather than folded into EXIT_CODES so the
# ordinary case stays a plain code->reason map, and so adding a prefixed code is
# one line rather than a change of shape for every entry.
PREFIXED_CODES: set[tuple[str, int]] = {("store", 5)}
