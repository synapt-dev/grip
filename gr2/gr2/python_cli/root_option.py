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

import re
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


class ReviewTargetCommand(RootOptionalCommand):
    """A review reader with an optional target and a workspace inferred from cwd.

    Two words retain ROOT TARGET. An explicit gr: target or bare hash retains
    its target role even when a directory has that name. Otherwise one existing
    directory names ROOT, and one other word names TARGET. With -C, the remaining
    word is TARGET. No target is chosen here: the reader refuses zero or several
    binds rather than choosing the most recent one.
    """

    def parse_args(self, ctx, args):
        args = list(args)
        positions, root_value = self._scan(args)
        if root_value is not None:
            if len(positions) > 1 or (positions and self._is_root_word(args[positions[0]])):
                ctx.fail("the workspace root was given twice: as the first argument and with --root/-C")
        elif len(positions) == 1 and not self._is_root_word(args[positions[0]]):
            from .app import _is_workspace_root, _resolve_workspace_root

            found = _resolve_workspace_root()
            if not _is_workspace_root(found):
                ctx.fail(f"no workspace at or above {found}; run this inside one, or name it with -C <root>")
            args.insert(positions[0], str(found))
        return super().parse_args(ctx, args)

    @staticmethod
    def _is_root_word(word: str) -> bool:
        target_spelled = word.startswith("gr:") or re.fullmatch(r"[0-9a-fA-F]{4,64}", word) is not None
        return not target_spelled and Path(word).is_dir()


class ContextCommand(RootOptionalCommand):
    """A verb that leads with the root and a unit and lets BOTH be left out, resolved by `context.py`.

    It rewrites the words into the full ``ROOT UNIT [REST...]`` form before click parses them, so everything
    downstream (the usage line, `--root/-C`, the given-twice refusals) is `RootOptionalCommand`'s. What it
    adds: `--unit NAME` names the unit; with the leading words left out they are inferred; when exactly one
    leading word is missing and no option names it, the first word is read as the root when it names a
    workspace, as the unit when it does not, and refused naming both readings when it could be either.
    The values and their sources land in ``ctx.meta["gr2.context"]`` for the verb to announce."""

    def parse_args(self, ctx, args):
        from . import context as c

        args = list(args)
        unit_value, args = self._take_unit_option(args, ctx)
        positions, root_value = self._scan(args)
        words = [args[i] for i in positions]
        required = self._required_positionals  # root, unit and the rest, all required for these verbs
        given = (root_value is not None) + (unit_value is not None)
        lead_words = len(words) - (required - 2)
        if unit_value is not None and lead_words > 1:
            ctx.fail("the unit was given twice: as an argument and with --unit")
        if lead_words == 2 and given == 0:
            ctx.meta["gr2.context"] = {
                "root": c.Resolved(words[0], "explicit"),
                "unit": c.Resolved(words[1], "explicit"),
            }
            return super().parse_args(ctx, args)
        if lead_words > 2 - given or lead_words < 0:
            # fully typed, or too few words: leave it to the class below, which refuses by name
            ctx.meta["gr2.context"] = {}
            return super().parse_args(ctx, [*args[:0], *args] if unit_value is None else self._restore(args, unit_value))
        try:
            root_item, unit_item, consumed = self._pick(c, words, lead_words, root_value, unit_value)
        except c.ContextRefused as exc:
            ctx.fail(str(exc))
        items = {"root": root_item, "unit": unit_item}
        ctx.meta["gr2.context"] = items
        at = positions[0] if positions else len(args)
        drop = set(positions[:consumed])
        rest = [t for i, t in enumerate(args) if i not in drop]
        before = sum(1 for i in range(at) if i not in drop)
        lead = [] if root_value is not None else [root_item.value]
        args_out = [*rest[:before], *lead, unit_item.value, *rest[before:]]
        return super().parse_args(ctx, args_out)

    @staticmethod
    def _restore(args, unit_value):
        return [*args, "--unit", unit_value]

    @staticmethod
    def _take_unit_option(args, ctx):
        out, value, i = [], None, 0
        while i < len(args):
            token = args[i]
            if token == "--unit":
                if i + 1 >= len(args):
                    ctx.fail("--unit needs a value")
                value, i = args[i + 1], i + 2
                continue
            if token.startswith("--unit="):
                value, i = token.partition("=")[2], i + 1
                continue
            out.append(token)
            i += 1
        return value, out

    @staticmethod
    def _pick(c, words, lead_words, root_value, unit_value):
        """(root, unit, leading words consumed) for the calls that leave a leading word out."""
        root_item = c.Resolved(root_value, "explicit") if root_value is not None else None
        unit_item = c.Resolved(unit_value, "explicit") if unit_value is not None else None
        consumed = 0
        if lead_words == 1:
            word = words[0]
            if root_item is not None and unit_item is None:
                unit_item, consumed = c.Resolved(word, "explicit"), 1
            elif unit_item is not None and root_item is None:
                root_item, consumed = c.Resolved(word, "explicit"), 1
            else:
                nearest = None
                try:
                    nearest = c.resolve_root(None)
                except c.ContextRefused:
                    pass
                is_root = Path(word).is_dir() and c._is_workspace_root(Path(word).resolve())
                known = set()
                if nearest is not None:
                    known = set(c.entered_units(Path(nearest.value))) | set(c.spec_units(Path(nearest.value)))
                if is_root and word in known:
                    raise c.ContextRefused(
                        f"{word!r} could be the workspace root or a unit; say which: "
                        f"-C {word} for the root, --unit {word} for the unit"
                    )
                if is_root:
                    root_item, consumed = c.Resolved(word, "explicit"), 1
                else:
                    unit_item, consumed = c.Resolved(word, "explicit"), 1
        if root_item is None:
            root_item = c.resolve_root(None)
        if unit_item is None:
            unit_item = c.resolve_unit(Path(root_item.value), None)
        return root_item, unit_item, consumed
