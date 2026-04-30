"""Shared fixtures for gr2 tests."""
from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with .grip/ directory."""
    grip = tmp_path / ".grip"
    grip.mkdir()
    events = grip / "events"
    events.mkdir()
    return tmp_path
