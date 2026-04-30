"""Scaffold tests for gr overlay CLI stubs.

Verifies that every subcommand exists, accepts expected arguments,
and exits with code 1 + 'not implemented' message (M1 stubs).
"""

from __future__ import annotations

from typer.testing import CliRunner

from gr2_overlay.cli import overlay_app

runner = CliRunner()


def test_activate_stub():
    result = runner.invoke(overlay_app, ["activate", "/tmp", "alice/theme-dark"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_deactivate_stub():
    result = runner.invoke(overlay_app, ["deactivate", "/tmp", "alice/theme-dark"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_diff_stub():
    result = runner.invoke(overlay_app, ["diff", "/tmp", "alice/theme-dark"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_list_stub():
    result = runner.invoke(overlay_app, ["list", "/tmp"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_stack_stub():
    result = runner.invoke(overlay_app, ["stack", "/tmp"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_status_stub():
    result = runner.invoke(overlay_app, ["status", "/tmp"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_trace_stub():
    result = runner.invoke(overlay_app, ["trace", "/tmp", "some/file.toml"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_why_stub():
    result = runner.invoke(overlay_app, ["why", "/tmp", "agent.name"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_impact_stub():
    result = runner.invoke(overlay_app, ["impact", "/tmp", "alice/theme-dark"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_activate_json_flag():
    result = runner.invoke(overlay_app, ["activate", "/tmp", "alice/theme-dark", "--json"])
    assert result.exit_code == 1


def test_list_json_flag():
    result = runner.invoke(overlay_app, ["list", "/tmp", "--json"])
    assert result.exit_code == 1


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
