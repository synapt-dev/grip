"""The settings every gr2 Typer app is built with.

A bare group (`gr2`, `gr2 lane`, `gr2 review`, ...) prints its help instead of a
"Missing command" error box, and `-h` is help at every level, beside `--help`.
Without these, a user's first command (`gr2` by itself, or `gr2 -h`) failed.
tests/test_cli_shape_witnesses.py walks the built app and fails on any group
built without them, so a new sub-app has to be made through `group_settings()`.
"""
from __future__ import annotations

from typing import Any


def group_settings() -> dict[str, Any]:
    """Keyword arguments for `typer.Typer(...)`; a fresh dict per app."""
    return {
        "no_args_is_help": True,
        "context_settings": {"help_option_names": ["-h", "--help"]},
    }
