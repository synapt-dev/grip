"""What a user reads about reviews says pin, stamp and repin, never the old words.

A rename swept by hand misses strings, and the miss is invisible to a test that only checks the aliases. These
rows read the text a user actually sees, every review verb's --help and the README's review sections, and refuse
the old words outside a short named allowlist.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()
OLD = re.compile(r"\b(bind|binds|bound|binder|binding|rebind|re-bind|approve|approves)\b", re.IGNORECASE)
ALIASES = {"bind", "approve", "rebind"}

# Lines allowed to keep an old word, each for a named reason.
ALLOWED = (
    re.compile(r"Alias of `review "),          # the alias rows name the old word on purpose
    re.compile(r"remain as aliases of `pin`"),  # the README sentence that names the aliases on purpose
    re.compile(r"^\| `review` \|"),            # the README verb table lists every live verb, aliases included
    re.compile(r"\blane bind\b|`lane bind`|--bind\b"),  # `lane bind` is a different verb, not renamed in this change
    re.compile(r"^\| `lane` \|"),               # the README lane verb table, same reason
    re.compile(r"\bbound-lane\b"),              # a lane made by `lane bind`, same reason
)


def _offending(text: str) -> list[str]:
    return [line for line in text.splitlines() if OLD.search(line) and not any(a.search(line) for a in ALLOWED)]


def _review_verbs() -> list[str]:
    group = typer.main.get_command(app).commands["review"]
    return sorted(name for name, cmd in group.commands.items() if not cmd.hidden and name not in ALIASES)


@pytest.mark.parametrize("verb", _review_verbs())
def test_review_verb_help_uses_the_new_words(verb):
    result = runner.invoke(app, ["review", verb, "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert _offending(result.output) == [], f"review {verb} --help still says an old word"


def test_review_group_help_uses_the_new_words():
    result = runner.invoke(app, ["review", "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert _offending(result.output) == []


def test_readme_review_text_uses_the_new_words():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    assert _offending(readme.read_text()) == [], "gr2/README.md still teaches an old review word"


def test_the_sweep_sees_an_old_word():
    # Control: the scanner is not blind. An old word on an unallowed line is reported; on an allowed line it is not.
    assert _offending("run gr2 review bind first") == ["run gr2 review bind first"]
    assert _offending("bind   Alias of `review pin`.") == []
    # The lane exception is exact: it allows `lane bind`, never a review word on a line that also says lane.
    assert _offending("reconstructs from a review-bind commit. The lane directory") != []
    assert _offending("see `lane bind` for a bound lane") == []
