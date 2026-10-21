"""Console entry point: build only the verb a call runs, the whole command tree only when it is needed.

Importing app.py builds every verb gr2 has, about 160 ms before any work starts. A call whose first
words are in TABLE builds that verb alone, under the same `gr2 <group>` path, so its parse, output
and exit are the full CLI's. Everything else, including --help anywhere on the line, an unknown verb
and `--version`, goes to the full tree unchanged.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional, Sequence


def _review_stamp():
    import typer

    from ..typer_settings import group_settings
    from .review_stamp import review_approve
    from .root_option import ReviewHeadCommand

    root = typer.Typer(**group_settings())
    review = typer.Typer(**group_settings())
    review.command("stamp", cls=ReviewHeadCommand)(review_approve)
    root.add_typer(review, name="review")
    return root


# (group, verb) -> builder of a CLI holding that verb alone at its full path.
TABLE: dict[tuple[str, str], Callable[[], object]] = {
    ("review", "stamp"): _review_stamp,
}


def route(argv: Sequence[str]) -> Optional[Callable[[], object]]:
    """The builder for this call, or None for the full tree."""
    if len(argv) < 2 or any(a in ("-h", "--help") for a in argv):
        return None
    return TABLE.get((argv[0], argv[1]))


def run(cli) -> None:
    """Run a CLI inside the one boundary every way in shares.

    An adapter's failure arrives as an exception that already carries the forge's own
    message — a remote that is not a forge URL, a head branch equal to its base.
    Uncaught, it reaches the user as a Python traceback in place of the refusal it
    already is, which is what a stranger met on the first-run path. Catching it here,
    once, covers every adapter a user may plug in rather than one check per trigger:
    adapters are exactly the part of this CLI the team does not write.
    """
    # `gr` runs this half with GR_RESOLVED set so it cannot resolve twice. The guard is spent once gr2 starts;
    # kept, it would reach every process gr2 runs, and any `gr` there would refuse to resolve twice.
    os.environ.pop("GR_RESOLVED", None)
    from .gr_shadow import note_shadow
    note_shadow()  # at most once per install, never under --json
    from .platform import AdapterError
    try:
        cli()
    except AdapterError as exc:
        import typer
        typer.echo(f"gr2: {exc}", err=True)
        raise SystemExit(1) from None


def main() -> None:
    """Console-script entry point."""
    build = route(sys.argv[1:])
    if build is None:
        from .app import main as full_main
        full_main()
        return
    run(build())
