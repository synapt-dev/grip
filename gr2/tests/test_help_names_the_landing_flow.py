"""The help text names the one flow that lands a reviewed change, and the review commands print their next step once."""
from __future__ import annotations

import re

from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()


def _plain(text: str) -> str:
    # CI forces colour and rich wraps panels; the words are what is under test.
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", text).replace("│", " ").split())


def test_top_level_help_names_the_flow_that_lands_a_reviewed_change():
    out = runner.invoke(app, ["--help"])
    assert out.exit_code == 0, out.output
    assert ("To land a reviewed change: gr2 review pin, publish, stamp, gr2 check, then gr2 review merge; "
            "the steps are in gr2 review --help.") in _plain(out.stdout), out.stdout
