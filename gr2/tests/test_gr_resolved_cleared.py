"""gr2 clears GR_RESOLVED on entry, so a process it starts can run `gr` again."""
from __future__ import annotations

import os

from gr2.python_cli import app as app_mod


def test_gr2_entry_clears_gr_resolved_before_any_command(monkeypatch) -> None:
    monkeypatch.setenv("GR_RESOLVED", "gr2")
    seen: list[str | None] = []
    monkeypatch.setattr(app_mod, "app", lambda: seen.append(os.environ.get("GR_RESOLVED")))
    app_mod.main()
    assert seen == [None]
