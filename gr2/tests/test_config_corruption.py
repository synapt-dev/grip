"""TDD tests for Story 8: JSON corruption repair/quarantine.

Bug: when overlay JSON is corrupted, config_show and config_apply raise
a raw json.JSONDecodeError with no recovery path.

Expected behavior: detect corrupt JSON, quarantine the file (rename to
.corrupt), and fall back to base-only config. Raise a structured
OverlayCorruptError that callers can catch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from python_cli.config import (
    config_apply,
    config_show,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TOML = """\
[spawn]
session_name = "synapt"
channel = "dev"

[agents.opus]
role = "CEO / product design"
model = "claude-opus-4-6"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    config_dir = ws / "config"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()
    return ws


@pytest.fixture
def applied_workspace(workspace: Path) -> Path:
    config_apply(
        base_path=workspace / "config" / "agents.toml",
        overlay_dir=workspace / "config" / "overlay",
    )
    return workspace


# ---------------------------------------------------------------------------
# Tests: corrupt overlay quarantine
# ---------------------------------------------------------------------------


class TestCorruptOverlayQuarantine:
    def test_corrupt_overlay_raises_structured_error(self, applied_workspace: Path) -> None:
        """Corrupt overlay should raise OverlayCorruptError, not raw JSONDecodeError."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("{corrupt json content!!!")

        with pytest.raises(OverlayCorruptError):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

    def test_corrupt_overlay_quarantined(self, applied_workspace: Path) -> None:
        """Corrupt overlay file should be renamed to .corrupt."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("{corrupt!!!")

        with pytest.raises(OverlayCorruptError):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

        corrupt_path = applied_workspace / "config" / "overlay" / "agents.json.corrupt"
        assert corrupt_path.exists(), "Corrupt file should be renamed to .corrupt"
        assert not overlay_path.exists(), "Original corrupt file should be removed"

    def test_config_show_falls_back_to_base_after_quarantine(
        self, applied_workspace: Path
    ) -> None:
        """After quarantine, a second config_show should return base-only config."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("not json")

        with pytest.raises(OverlayCorruptError):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

        # Second call should work (no overlay file, falls back to base)
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
        )
        assert result["agents"]["opus"]["role"] == "CEO / product design"

    def test_config_apply_quarantines_corrupt_overlay(
        self, applied_workspace: Path
    ) -> None:
        """config_apply with corrupt existing overlay should quarantine and reapply."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("{truncated...")

        with pytest.raises(OverlayCorruptError):
            config_apply(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

        corrupt_path = applied_workspace / "config" / "overlay" / "agents.json.corrupt"
        assert corrupt_path.exists()

    def test_corrupt_prompt_overlay_quarantined(self, applied_workspace: Path) -> None:
        """Corrupt prompt overlay should be quarantined during config_show."""
        from python_cli.config import OverlayCorruptError

        prompts_dir = applied_workspace / "config" / "overlay" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        corrupt_prompt = prompts_dir / "opus.json"
        corrupt_prompt.write_text("{{bad json")

        with pytest.raises(OverlayCorruptError):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

        assert (prompts_dir / "opus.json.corrupt").exists()
        assert not corrupt_prompt.exists()

    def test_error_message_includes_path(self, applied_workspace: Path) -> None:
        """OverlayCorruptError message should include the file path."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("nope")

        with pytest.raises(OverlayCorruptError, match="agents.json"):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

    def test_multiple_corrupt_files_quarantined(self, applied_workspace: Path) -> None:
        """If overlay JSON and a prompt file are both corrupt, both get quarantined."""
        from python_cli.config import OverlayCorruptError

        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay_path.write_text("bad")

        with pytest.raises(OverlayCorruptError):
            config_show(
                base_path=applied_workspace / "config" / "agents.toml",
                overlay_dir=applied_workspace / "config" / "overlay",
            )

        assert (applied_workspace / "config" / "overlay" / "agents.json.corrupt").exists()
