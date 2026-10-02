"""The workspace root as an option, and, for fixed-arity verbs, as a word that may be left out.

Every workspace verb used to take the root as its FIRST required positional, so a caller inside a
workspace retyped a path the current directory already named. Click binds positionals in order, so an
optional leading argument followed by required ones cannot be declared. Two command classes cover it,
and neither reads what a positional contains to decide anything about a root:

* ``RootOptionCommand``: ``--root/-C`` names the root; the positional form is untouched, and a bare
  call is NOT inferred. For verbs whose last positional is optional, one fewer word has two readings
  (``verb unit lane`` is both "root omitted" and "root = unit"), so the root is never guessed there.
* ``RootOptionalCommand``: also COUNTS. One fewer positional than the verb requires means the root is
  the missing one, and it is put in front: from ``--root/-C`` when given, else the nearest workspace
  above the current directory. Exact for a verb with a fixed number of positionals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
import typer.core

ROOT_OPTION = typer.Option(
    None,
    "--root",
    "-C",
    help="The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.",
)

_HINT = "pass the workspace root first, or name it with -C <root>"


def _usage_error():
    try:
        from typer._click.exceptions import UsageError
    except ImportError:  # an older typer, which uses the installed click
        from click.exceptions import UsageError
    return UsageError


class RootOptionCommand(typer.core.TyperCommand):
    """``--root/-C`` names the workspace root; a bare call is not inferred (see the module docstring)."""

    infer_bare = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        arguments = [p for p in self.params if p.param_type_name == "argument"]
        self._required_positionals = sum(1 for p in arguments if p.required)
        self._optional_positionals = sum(1 for p in arguments if not p.required)
        if self.infer_bare:
            # The root is always supplied before click sees the arguments, so it is not required of
            # the caller: say so in the usage line and the help.
            for param in arguments:
                if param.name == "workspace_root":
                    param.required = False
                    break

    def _scan(self, args: list[str]) -> tuple[list[int], Optional[str]]:
        """Indexes of the positional tokens, and the value of ``--root/-C`` if one was given."""
        value_options = {
            name
            for p in self.params
            if p.param_type_name == "option" and not getattr(p, "is_flag", False) and p.nargs == 1
            for name in (*p.opts, *p.secondary_opts)
        }
        positions: list[int] = []
        root_value: Optional[str] = None
        i = 0
        while i < len(args):
            token = args[i]
            if token == "--":
                positions.extend(range(i + 1, len(args)))
                break
            if token.startswith("-") and token != "-":
                name, eq, inline = token.partition("=")
                if token.startswith("-C") and not token.startswith("--"):
                    # click's short option: `-C ROOT` or glued `-CROOT` (everything after the letter is the value)
                    if len(token) > 2:
                        root_value = token[2:]
                    elif i + 1 < len(args):
                        root_value = args[i + 1]
                        i += 1
                elif name == "--root":
                    if eq:
                        root_value = inline
                    elif i + 1 < len(args):
                        root_value = args[i + 1]
                        i += 1
                elif not eq and name in value_options:
                    i += 1
                i += 1
                continue
            positions.append(i)
            i += 1
        return positions, root_value

    def parse_args(self, ctx, args):
        args = list(args)
        required = self._required_positionals  # counted before the root is made optional, so it includes the root
        optional = self._optional_positionals
        positions, root_value = self._scan(args)
        count = len(positions)
        at = positions[0] if positions else len(args)
        if root_value is not None:
            if count > required - 1 + optional:
                ctx.fail("the workspace root was given twice: as the first argument and with --root/-C")
            args.insert(at, root_value)
        elif self.infer_bare and count == required - 1:
            from .app import _is_workspace_root, _resolve_workspace_root

            found = _resolve_workspace_root()
            if not _is_workspace_root(found):
                ctx.fail(f"no workspace at or above {found}; run this inside one, or name it with -C <root>")
            args.insert(at, str(found))
        elif not self.infer_bare:
            if count >= required and not Path(args[positions[0]]).is_dir():
                ctx.fail(f"{args[positions[0]]!r} is not a directory, so it cannot be the workspace root; {_HINT}")
        try:
            remaining = super().parse_args(ctx, args)
            # The backstop: whatever form the scan above did not recognise, click still parsed it as the
            # root option. If that disagrees with the root the verb is about to use, the root was named one
            # way and placed another, so refuse rather than act on the wrong workspace.
            given = ctx.params.get("root")
            if given is not None and Path(str(ctx.params.get("workspace_root"))) != Path(str(given)):
                ctx.fail("the workspace root was given in a form gr2 could not place; use -C <root> with a space")
            return remaining
        except _usage_error() as exc:
            # Only a missing ARGUMENT can mean the root was read as the first word; a missing option cannot.
            if self.infer_bare or root_value is not None or not exc.format_message().startswith("Missing argument"):
                raise
            ctx.fail(f"{exc.format_message().rstrip('.')}. The first argument is the workspace root: {_HINT}")


class RootOptionalCommand(RootOptionCommand):
    """``--root/-C``, and a leading workspace root that may be left out (counted, never guessed)."""

    infer_bare = True
