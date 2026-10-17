"""Witness that the seat's GR2_QUIET_CONTEXT is dropped in this SIBLING testpath too.

Same shape as `test_conftest_git_isolation_overlay.py`: `gr2/overlay/tests/` is
not a descendant of `gr2/tests/`, so a fixture placed in `gr2/tests/conftest.py`
never runs here. The fixture has to live in the root `gr2/conftest.py`.
"""
from __future__ import annotations

import os

import pytest


def test_the_seat_quiet_context_is_dropped_here_too(request: pytest.FixtureRequest) -> None:
    """The fixture is active for this test, and the variable is gone.

    The first assert holds on every host; the second only bites on a host that
    exports GR2_QUIET_CONTEXT, which is the host the fixture exists for.
    """
    assert "_no_seat_quiet_context" in request.fixturenames
    assert "GR2_QUIET_CONTEXT" not in os.environ
