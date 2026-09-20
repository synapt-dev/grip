"""Shared fixtures for gr2 tests.

The suite-wide `_isolated_git_config` autouse fixture lives in the ROOT
conftest (`gr2/conftest.py`), not here -- it must be an ancestor of both
`tests/` and the sibling `gr2/overlay/tests/` tree, and this directory is
not.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from typer.testing import CliRunner


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with .grip/ directory."""
    grip = tmp_path / ".grip"
    grip.mkdir()
    events = grip / "events"
    events.mkdir()
    return tmp_path


def make_cli_runner() -> CliRunner:
    """A runner whose ``result.stdout`` is the command's stdout ALONE, on every
    click version.

    click<8.2 folds stderr into stdout unless ``mix_stderr=False``; click>=8.2
    removed ``mix_stderr`` and separates the streams by default (passing the kwarg
    raises ``TypeError``). Without this, ``json.loads(result.stdout)`` reds under
    click<8.2 whenever the command also writes a stderr warning (e.g. an
    UNVERIFIABLE merge-parent verdict prepended to the JSON payload) and greens
    under click>=8.2 -- a gate that flips on the ambient click version, not on the
    code under test. Two agents parse ``--json`` output as data, so the assertion
    must be pinned to the data channel regardless of the installed click.

    Lifted from test_sprint21_sync_platform.py's private ``_make_runner``
    into one shared copy so every test file that parses a command's stdout as JSON
    gets the same version-safety, rather than seven near-identical private copies
    drifting apart.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()
