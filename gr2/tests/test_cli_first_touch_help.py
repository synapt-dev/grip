"""A user's first gr2 command prints help.

Before typer_settings.group_settings(), `gr2` by itself and every bare group printed a
"Missing command" error box, and `-h` was "No such option" at every level, on PyPI and on
dev alike. These rows run the forms a first-time user types. The walk in
test_cli_shape_witnesses.py covers every group, including ones added later.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

ERRORS = ("Missing command", "No such option", "No such command")


@pytest.mark.parametrize(
    "args",
    [[], ["lane"], ["review"], ["store"], ["lane", "lease"]],
    ids=lambda a: "gr2 " + " ".join(a) if a else "gr2",
)
def test_bare_group_prints_its_help(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert "Usage:" in result.output, result.output
    assert not any(e in result.output for e in ERRORS), result.output


@pytest.mark.parametrize(
    "args",
    [["-h"], ["lane", "-h"], ["lane", "create", "-h"], ["review", "-h"], ["config", "-h"]],
    ids=lambda a: "gr2 " + " ".join(a),
)
def test_dash_h_is_help_at_every_level(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output, result.output
    assert not any(e in result.output for e in ERRORS), result.output
