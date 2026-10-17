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


def test_review_help_lets_publish_push_the_heads_and_check_set_alone_satisfy_merge():
    out = runner.invoke(app, ["review", "--help"])
    assert out.exit_code == 0, out.output
    text = _plain(out.stdout)
    assert "push the head" not in text, out.stdout
    assert "five steps" in text and "2. gr2 review publish push every member's pinned head to its own remote" in text
    assert ("a review over several members needs only gr2 check set, one command over every member: merge accepts on "
            "the set alone") in text, out.stdout
