"""Tests for gr overlay CLI command registration and argument shapes.

Verifies that all 9 subcommands exist with expected argument signatures.
The actual wiring behavior is tested in test_overlay_cli_wiring.py.
"""

from __future__ import annotations

from typer.testing import CliRunner

from gr2_overlay.cli import overlay_app

runner = CliRunner()


def test_subcommand_count():
    """Verify all 9 expected subcommands are registered."""
    expected = {
        "activate",
        "deactivate",
        "diff",
        "list",
        "stack",
        "status",
        "trace",
        "why",
        "impact",
    }
    registered = {cmd.name for cmd in overlay_app.registered_commands}
    assert registered == expected


def test_activate_requires_ref_argument():
    result = runner.invoke(overlay_app, ["activate", "/tmp"])
    assert result.exit_code != 0


def test_deactivate_requires_ref_argument():
    result = runner.invoke(overlay_app, ["deactivate", "/tmp"])
    assert result.exit_code != 0


def test_diff_requires_ref_argument():
    result = runner.invoke(overlay_app, ["diff", "/tmp"])
    assert result.exit_code != 0


def test_trace_requires_file_path_argument():
    result = runner.invoke(overlay_app, ["trace", "/tmp"])
    assert result.exit_code != 0


def test_why_requires_key_argument():
    result = runner.invoke(overlay_app, ["why", "/tmp"])
    assert result.exit_code != 0


def test_impact_requires_ref_argument():
    result = runner.invoke(overlay_app, ["impact", "/tmp"])
    assert result.exit_code != 0


def test_list_accepts_workspace_root_only():
    result = runner.invoke(overlay_app, ["list", "/tmp"])
    assert result.exit_code == 0


def test_stack_accepts_workspace_root_only():
    result = runner.invoke(overlay_app, ["stack", "/tmp"])
    assert result.exit_code == 0


def test_status_accepts_workspace_root_only():
    result = runner.invoke(overlay_app, ["status", "/tmp"])
    assert result.exit_code == 0
