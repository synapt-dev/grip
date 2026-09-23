"""Two shape properties of the CLI that no per-command test can see.

Both were found by the head seat's review of a change that made `workspace_root`
optional across many verbs, and neither could have been caught by the Python
syntax rule that produced the change:

1. A command whose function body runs any statement before its docstring loses
   its help text entirely, because Typer reads the docstring as the first
   statement. One command had a line inserted above its docstring and became the
   only visible command with empty help out of 69.
2. `workspace_root: Optional[Path] = typer.Argument(None)` followed by
   `owner_unit: str = typer.Argument()` is legal PYTHON — both have a default,
   the second being an `ArgumentInfo` object — while Typer treats the second as
   REQUIRED and binds positionally, so the implied form hands the first argument
   to the root and then reports a missing one it never got. Six review verbs were
   in this state and the ast walk could not see it, because the constraint lives
   in Typer's model and not in Python's.

These walk the built Typer app rather than the source, so they see what a user
sees. Each has an in-process mutation that registers a deliberately defective
command and requires the predicate to report it — a witness whose predicate
cannot fail proves nothing, and the two defects above are both real instances
that these would have caught.
"""
from __future__ import annotations

from pathlib import Path

import typer
import typer.main

from gr2.python_cli.app import app


def _walk(group, path: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for name, cmd in sorted(getattr(group, "commands", {}).items()):
        here = f"{path} {name}".strip()
        if hasattr(cmd, "commands"):
            out.extend(_walk(cmd, here))
        else:
            out.append((here, cmd))
    return out


def _commands(application=app) -> list[tuple[str, object]]:
    return _walk(typer.main.get_command(application))


def _without_help(cmds) -> list[str]:
    return [
        path
        for path, cmd in cmds
        if not ((cmd.help or "") + (getattr(cmd, "short_help", "") or "")).strip()
    ]


def _optional_positional_before_required(cmds) -> list[tuple[str, str, list[str]]]:
    offenders = []
    for path, cmd in cmds:
        args = [p for p in cmd.params if type(p).__name__ == "TyperArgument"]
        for i, arg in enumerate(args):
            if arg.required:
                continue
            after = [p.name for p in args[i + 1:] if p.required]
            if after:
                offenders.append((path, arg.name, after))
                break
    return offenders


def test_visible_command_count_is_reported_for_the_control_below() -> None:
    """Not a property, a fixture guard: the walk must see the real app, or the
    two witnesses below pass by seeing nothing at all."""
    cmds = _commands()
    assert len(cmds) > 40, f"the walk found only {len(cmds)} commands; it is not seeing the app"


def test_every_visible_command_has_help_text() -> None:
    offenders = _without_help(_commands())
    assert not offenders, f"commands with no help text: {offenders}"


def test_the_help_witness_reports_a_command_with_no_help() -> None:
    """The mutation: register a command with no docstring and require the
    predicate to name it. Without this the witness above could be blind and
    still green — which is exactly what it was before the fix, when one command
    had lost its help and nothing noticed.

    The probe hangs off a sub-app on purpose: the walk reads `group.commands`,
    so a bare single-command Typer returns the command itself and the walk sees
    nothing. Wrapping it exercises the same path the real app takes."""
    outer = typer.Typer()
    inner = typer.Typer()
    inner.command("no-help-here")(lambda: None)
    outer.add_typer(inner, name="probe")
    offenders = _without_help(_walk(typer.main.get_command(outer)))
    assert any(o.endswith("no-help-here") for o in offenders), offenders

    outer_clean = typer.Typer()
    inner_clean = typer.Typer()

    def documented() -> None:
        """Has help."""

    inner_clean.command("documented")(documented)
    outer_clean.add_typer(inner_clean, name="probe")
    assert _without_help(_walk(typer.main.get_command(outer_clean))) == []


def test_no_optional_positional_precedes_a_required_one() -> None:
    offenders = _optional_positional_before_required(_commands())
    assert not offenders, (
        "optional positional before a required one (Typer binds in order, so the "
        f"implied form mis-binds): {offenders}"
    )


def test_the_order_witness_reports_a_leading_optional_positional() -> None:
    """The mutation, in the shape the six review verbs actually had."""
    outer = typer.Typer()
    inner = typer.Typer()

    def mis_ordered(
        workspace_root: Path | None = typer.Argument(None),
        commit: str = typer.Argument(),
    ) -> None:
        """Probe."""

    inner.command("mis-ordered")(mis_ordered)
    outer.add_typer(inner, name="probe")
    found = _optional_positional_before_required(_walk(typer.main.get_command(outer)))
    assert found and found[0][1] == "workspace_root", found
    assert found[0][2] == ["commit"], found

    outer_clean = typer.Typer()
    inner_clean = typer.Typer()

    def ordered(
        commit: str = typer.Argument(),
        workspace_root: Path | None = typer.Argument(None),
    ) -> None:
        """Probe."""

    inner_clean.command("ordered")(ordered)
    outer_clean.add_typer(inner_clean, name="probe")
    assert _optional_positional_before_required(_walk(typer.main.get_command(outer_clean))) == []
