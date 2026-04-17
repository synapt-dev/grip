"""Tests for gr2 apply convergence (grip#539).

Tests that build_plan() detects missing repo checkouts inside existing units
and that apply_plan() converges them idempotently.

Acceptance criteria:
- Planning emits an operation when declared unit repos are absent even if
  unit.toml and unit path exist
- Apply clones/converges the missing nested repos idempotently
- Regression test covers the scenario
"""
from __future__ import annotations

import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from gr2.python_cli.spec_apply import (
    build_plan,
    apply_plan,
    render_unit_toml,
    workspace_spec_path,
    repo_cache_path,
)


class ConvergenceTestBase(unittest.TestCase):
    """Base class that creates a minimal workspace with unit metadata."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp)

        grip_dir = self.workspace / ".grip"
        grip_dir.mkdir(parents=True)

        self.repo_specs = [
            {"name": "repo-a", "path": "repos/repo-a", "url": "https://example.com/repo-a.git"},
            {"name": "repo-b", "path": "repos/repo-b", "url": "https://example.com/repo-b.git"},
        ]

        self.unit_spec = {
            "name": "test-unit",
            "path": "agents/test-unit",
            "repos": ["repo-a", "repo-b"],
        }

        self._write_spec(self.repo_specs, [self.unit_spec])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_spec(self, repos, units):
        lines = ['workspace_name = "test-workspace"', ""]
        for repo in repos:
            lines.extend([
                "[[repos]]",
                f'name = "{repo["name"]}"',
                f'path = "{repo["path"]}"',
                f'url = "{repo["url"]}"',
                "",
            ])
        for unit in units:
            repos_str = "[" + ", ".join(f'"{r}"' for r in unit["repos"]) + "]"
            lines.extend([
                "[[units]]",
                f'name = "{unit["name"]}"',
                f'path = "{unit["path"]}"',
                f"repos = {repos_str}",
                "",
            ])
        workspace_spec_path(self.workspace).write_text("\n".join(lines))

    def _create_unit_on_disk(self, unit, *, with_repos=None):
        """Create unit dir + unit.toml, optionally with repo checkout dirs."""
        unit_root = self.workspace / unit["path"]
        unit_root.mkdir(parents=True, exist_ok=True)
        (unit_root / "unit.toml").write_text(render_unit_toml(unit))
        if with_repos:
            for repo_name in with_repos:
                repo_dir = unit_root / repo_name
                repo_dir.mkdir(parents=True, exist_ok=True)
                (repo_dir / ".git").mkdir()

    def _create_workspace_repo(self, repo):
        """Create a fake workspace-level repo directory."""
        path = self.workspace / repo["path"]
        path.mkdir(parents=True, exist_ok=True)
        (path / ".git").mkdir()

    def _create_repo_cache(self, repo_name):
        """Create a fake bare repo cache directory."""
        cache = repo_cache_path(self.workspace, repo_name)
        cache.mkdir(parents=True, exist_ok=True)

    def _fully_materialize(self):
        """Set up workspace as if initial apply completed: repos, caches, unit with checkouts."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        self._create_unit_on_disk(self.unit_spec, with_repos=["repo-a", "repo-b"])


class TestBuildPlanConvergence(ConvergenceTestBase):
    """Tests that build_plan detects missing repo checkouts inside existing units."""

    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    def test_detects_missing_unit_repo_checkouts(self, _repo, _dir, _hooks):
        """Unit dir + unit.toml exist but repos inside unit missing -> converge_unit_repos."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        self._create_unit_on_disk(self.unit_spec, with_repos=[])

        _, operations = build_plan(self.workspace)

        converge_ops = [op for op in operations if op.kind == "converge_unit_repos"]
        self.assertEqual(len(converge_ops), 1)
        self.assertEqual(converge_ops[0].subject, "test-unit")
        self.assertIn("repo-a", converge_ops[0].details["missing_repos"])
        self.assertIn("repo-b", converge_ops[0].details["missing_repos"])

    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    def test_detects_partial_missing_repos(self, _repo, _dir, _hooks):
        """Only some repos missing inside unit -> converge lists only the missing ones."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        self._create_unit_on_disk(self.unit_spec, with_repos=["repo-a"])

        _, operations = build_plan(self.workspace)

        converge_ops = [op for op in operations if op.kind == "converge_unit_repos"]
        self.assertEqual(len(converge_ops), 1)
        self.assertEqual(converge_ops[0].details["missing_repos"], ["repo-b"])

    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    def test_no_op_when_fully_materialized(self, _repo, _dir, _hooks):
        """All repos present inside unit -> no converge operation."""
        self._fully_materialize()

        _, operations = build_plan(self.workspace)

        converge_ops = [op for op in operations if op.kind == "converge_unit_repos"]
        self.assertEqual(len(converge_ops), 0)
        self.assertEqual(len(operations), 0)

    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    def test_new_unit_still_uses_create_and_write(self, _repo, _dir, _hooks):
        """Brand-new unit (no dir, no toml) uses create_unit_root + write_unit_metadata, not converge."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])

        _, operations = build_plan(self.workspace)

        kinds = [op.kind for op in operations]
        self.assertIn("create_unit_root", kinds)
        self.assertIn("write_unit_metadata", kinds)
        self.assertNotIn("converge_unit_repos", kinds)

    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    def test_stale_unit_toml_triggers_converge(self, _repo, _dir, _hooks):
        """Unit.toml lists fewer repos than spec -> converge for the new repo."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        stale_unit = {**self.unit_spec, "repos": ["repo-a"]}
        self._create_unit_on_disk(stale_unit, with_repos=["repo-a"])

        _, operations = build_plan(self.workspace)

        converge_ops = [op for op in operations if op.kind == "converge_unit_repos"]
        self.assertEqual(len(converge_ops), 1)
        self.assertEqual(converge_ops[0].details["missing_repos"], ["repo-b"])


class TestApplyConvergence(ConvergenceTestBase):
    """Tests that apply_plan handles converge_unit_repos correctly."""

    @patch("gr2.python_cli.spec_apply.run_lifecycle_stage")
    @patch("gr2.python_cli.spec_apply.apply_file_projections", return_value=[])
    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    @patch("gr2.python_cli.spec_apply.clone_repo", return_value=True)
    def test_apply_clones_missing_repos_into_unit(self, mock_clone, _repo, _dir, _hooks, _proj, _lc):
        """Apply should clone missing repos into the unit directory."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        self._create_unit_on_disk(self.unit_spec, with_repos=[])

        result = apply_plan(self.workspace, yes=True)

        self.assertGreater(result["operation_count"], 0)
        clone_calls = mock_clone.call_args_list
        unit_root = self.workspace / "agents" / "test-unit"
        expected_targets = {unit_root / "repo-a", unit_root / "repo-b"}
        actual_targets = set()
        for c in clone_calls:
            args, kwargs = c
            actual_targets.add(args[1])
        self.assertTrue(expected_targets.issubset(actual_targets))

    @patch("gr2.python_cli.spec_apply.run_lifecycle_stage")
    @patch("gr2.python_cli.spec_apply.apply_file_projections", return_value=[])
    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    @patch("gr2.python_cli.spec_apply.clone_repo", return_value=True)
    def test_apply_updates_stale_unit_toml(self, _clone, _repo, _dir, _hooks, _proj, _lc):
        """After convergence, unit.toml should reflect the full spec repo list."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        stale_unit = {**self.unit_spec, "repos": ["repo-a"]}
        self._create_unit_on_disk(stale_unit, with_repos=["repo-a"])

        apply_plan(self.workspace, yes=True)

        unit_toml = self.workspace / "agents" / "test-unit" / "unit.toml"
        content = unit_toml.read_text()
        self.assertIn("repo-a", content)
        self.assertIn("repo-b", content)

    @patch("gr2.python_cli.spec_apply.run_lifecycle_stage")
    @patch("gr2.python_cli.spec_apply.apply_file_projections", return_value=[])
    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    @patch("gr2.python_cli.spec_apply.clone_repo", return_value=True)
    def test_convergence_is_idempotent(self, mock_clone, _repo, _dir, _hooks, _proj, _lc):
        """After apply, a second build_plan should show no converge operations."""
        self._fully_materialize()

        _, operations = build_plan(self.workspace)

        self.assertEqual(len(operations), 0)
        mock_clone.assert_not_called()

    @patch("gr2.python_cli.spec_apply.run_lifecycle_stage")
    @patch("gr2.python_cli.spec_apply.apply_file_projections", return_value=[])
    @patch("gr2.python_cli.spec_apply.load_repo_hooks", return_value=None)
    @patch("gr2.python_cli.spec_apply.is_git_dir", return_value=True)
    @patch("gr2.python_cli.spec_apply.is_git_repo", return_value=True)
    @patch("gr2.python_cli.spec_apply.clone_repo", return_value=True)
    def test_apply_reports_converged_repos(self, _clone, _repo, _dir, _hooks, _proj, _lc):
        """Apply result should list what was converged."""
        for repo in self.repo_specs:
            self._create_workspace_repo(repo)
            self._create_repo_cache(repo["name"])
        self._create_unit_on_disk(self.unit_spec, with_repos=[])

        result = apply_plan(self.workspace, yes=True)

        converge_actions = [a for a in result["applied"] if "converge" in a.lower()]
        self.assertGreater(len(converge_actions), 0)


if __name__ == "__main__":
    unittest.main()
