"""`review pin --help` must not call a row's source optional.

A multi-row pin refuses any row that carries neither a source nor a range."""
from __future__ import annotations

from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_review_a4 import _flat


def _rows_json_help() -> str:
    wide = {"COLUMNS": "400", "TERMINAL_WIDTH": "400"}
    result = make_cli_runner().invoke(app, ["review", "pin", "--help"], env=wide)
    assert result.exit_code == 0, result.output
    text = _flat(result.stdout)
    start = text.index("--rows-json <path>")
    return text[start:text.index(" --", start + len("--rows-json <path>"))]


def test_rows_json_help_does_not_call_source_optional():
    assert "optional path/ref/title/body/source" not in _rows_json_help()


def test_rows_json_help_says_every_row_needs_source_or_range_when_several():
    help_text = _rows_json_help()
    assert "source" in help_text and "range" in help_text
    assert "required on every row" in help_text and "more than one" in help_text
