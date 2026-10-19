"""`review open-project` requires a project-review-KIND gr commit, and until now NO
CLI verb produced one — a stranger who did not author the code had no path from help
text to a multi-repo review. `review create-project` is the producer half of "one gr
commit opens one exact multi-repo review": from a materialized lane it prints the
``gr:<sha>`` that `review open-project` consumes.

These tests are the stranger acceptance: the verb exists, produces a consumable
commit pinned at the recorded fork base, refuses cleanly, and is discoverable from
help text alone.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip

from .test_review_bind_fork_base import _materialized_lane, _commit_lane_change

runner = CliRunner()


def _gr_sha(stdout: str) -> str:
    line = next(l for l in stdout.splitlines() if l.startswith("gr:"))
    return line[3:].strip()


def test_create_project_prints_a_consumable_gr_commit(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    result = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature"])
    assert result.exit_code == 0, result.output
    sha = _gr_sha(result.output)
    # the printed sha is exactly what open-project decodes, and its base is the
    # recorded fork base (never HEAD^) for each pinned repo.
    doc = lanes.load_lane_doc(ws, "atlas", "feature")
    rows = {r["key"]: r for r in grip.read_project_review_commit(ws, sha)}
    assert set(rows) == {"a", "b"}
    for r in ("a", "b"):
        assert rows[r]["base"] == doc["fork_base"][r]["sha"]


def test_create_project_refuses_a_lane_with_no_fork_base_cleanly(tmp_path: Path) -> None:
    # A lane with no recorded fork base must refuse (exit 2), not traceback and not
    # silently pin against a guessed base.
    ws = _materialized_lane(tmp_path, ["a"], fork_base=False)
    result = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature"])
    assert result.exit_code == 2, result.output
    assert "refused" in result.output.lower()


def test_create_project_is_discoverable_from_help(tmp_path: Path) -> None:
    # The door fix: a stranger reading help alone can find the producer. It is
    # plumbing since the help surface split, so `gr2 help -a` lists it, and
    # open-project's arg help names it.
    help_all = runner.invoke(gr2_app.app, ["help", "-a"]).output
    assert "gr2 review create-project " in help_all
    open_help = runner.invoke(gr2_app.app, ["review", "open-project", "--help"]).output
    assert "create-project" in open_help
