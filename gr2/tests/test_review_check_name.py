"""The visible check spelling and the requirements alias run the same read-only check.

Rows go red if either name changes the compiled counts, the alias contaminates JSON,
root inference is lost, the new name is hidden, or the old name becomes visible.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.prototypes import lane_workspace_prototype as lane_proto

runner = CliRunner()


def _workspace(tmp_path: Path, reviewers: int) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip/workspace_spec.toml").write_text(
        'schema_version = 1\n[workspace_constraints.required_reviewers]\nalpha = 1\n'
    )
    for index in range(reviewers):
        path = lane_proto.lane_state_root(ws) / f"reader{index}" / "review" / "lane.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            f'owner_unit = "reader{index}"\nlane_name = "review"\nlane_type = "review"\n'
            'repos = ["alpha"]\n[[pr_associations]]\nref = "alpha#7"\n'
        )
    return ws


@pytest.mark.parametrize("reviewers", [0, 1])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_check_reports_real_compiled_counts_and_preserves_the_alias_result(tmp_path, reviewers, flags):
    ws = _workspace(tmp_path, reviewers)
    before = {str(p.relative_to(ws)): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    checked = runner.invoke(app, ["review", "check", str(ws), "alpha", "7", *flags])
    legacy = runner.invoke(app, ["review", "requirements", str(ws), "alpha", "7", *flags])
    assert checked.exit_code == legacy.exit_code == 0, (checked.output, legacy.output)
    doc = json.loads(checked.stdout)
    assert json.loads(legacy.stdout) == doc
    assert doc["required_reviewers"] == 1 and doc["actual_reviewers"] == reviewers
    assert doc["satisfied"] is bool(reviewers)
    assert "deprecated" not in checked.stderr
    assert "review requirements is deprecated; use review check (removed at beta)" in legacy.stderr
    after = {str(p.relative_to(ws)): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("verb", ["check", "requirements"])
def test_both_spellings_infer_the_nearest_workspace(tmp_path, monkeypatch, verb):
    ws = _workspace(tmp_path, 1)
    nested = ws / "sub" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    result = runner.invoke(app, ["review", verb, "alpha", "7", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["satisfied"] is True


def test_review_help_lists_check_and_hides_the_old_spelling():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0
    command_lines = [line for line in result.stdout.splitlines() if line.lstrip(" │").startswith("check ")]
    assert command_lines
    assert not [line for line in result.stdout.splitlines() if line.lstrip(" │").startswith("requirements ")]
