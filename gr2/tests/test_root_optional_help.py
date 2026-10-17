"""Every verb whose workspace root may be left out prints its --help outside a workspace.

`--help` asks about the verb, not about a workspace, so it must not need one. A verb on
`RootOptionalCommand` resolves the root from the cwd before click sees its words, and the help
short-circuit used to live only in `ReviewTargetCommand`; every other such verb, run with --help
from a directory that is not inside a workspace, refused with "no workspace at or above ..." and
exit 2. This row walks the whole typer app, so a verb added on the class later is covered too.
"""
from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.root_option import RootOptionalCommand

runner = CliRunner()


def _walk(group, prefix=()):
    for name, cmd in sorted(group.commands.items()):
        path = (*prefix, name)
        if isinstance(cmd, RootOptionalCommand):
            yield path
        if hasattr(cmd, "commands"):
            yield from _walk(cmd, path)


ROOT_OPTIONAL_VERBS = sorted(_walk(typer.main.get_command(app)))


def test_the_walk_finds_the_verbs():
    # Control: the walk is not empty, and it reaches the two verbs the failure was first seen on.
    assert ("lane", "list") in ROOT_OPTIONAL_VERBS
    assert ("review", "list") in ROOT_OPTIONAL_VERBS


@pytest.mark.parametrize("verb", ROOT_OPTIONAL_VERBS, ids=" ".join)
def test_help_needs_no_workspace(verb, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*verb, "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
