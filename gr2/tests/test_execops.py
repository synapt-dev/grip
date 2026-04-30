"""Tests for gr2 exec lane-aware execution (grip#544).

Tests the execution surface defined in GR2-MVP.md Gap 1:
- Lane-scoped execution with repo filtering
- Parallel vs sequential execution policy
- Fail-fast vs collect-all behavior
- Structured result reporting
- Event emission (exec.started, exec.completed, exec.failed)
"""
from __future__ import annotations

import json
import os
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from gr2.python_cli.events import EventType, _outbox_path
from gr2.python_cli.execops import run_exec, ExecResult, run_exec_parallel


class ExecTestBase(unittest.TestCase):
    """Base class that sets up a minimal workspace with lane metadata."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp)
        self.owner_unit = "test-unit"
        self.lane_name = "test-lane"
        self.actor = "agent:test"

        lane_base = self.workspace / "agents" / self.owner_unit / "lanes" / self.lane_name
        self.repos = ["repo-a", "repo-b", "repo-c"]
        for repo in self.repos:
            repo_dir = lane_base / "repos" / repo
            repo_dir.mkdir(parents=True, exist_ok=True)

        self.lane_doc = {
            "lane_name": self.lane_name,
            "owner_unit": self.owner_unit,
            "lane_type": "feature",
            "repos": self.repos,
            "branch_map": {"repo-a": "feat/x", "repo-b": "feat/x", "repo-c": "feat/x"},
            "exec_defaults": {
                "parallelism": "sequential",
                "fail_fast": True,
                "commands": [],
            },
            "context": {"shared_roots": [], "private_roots": []},
        }

        events_dir = self.workspace / ".grip" / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_events(self) -> list[dict]:
        outbox = _outbox_path(self.workspace)
        if not outbox.exists():
            return []
        events = []
        for line in outbox.read_text().strip().split("\n"):
            if line.strip():
                events.append(json.loads(line))
        return events

    def _mock_lane_proto(self):
        mock = MagicMock()
        mock.load_current_lane_doc.return_value = {
            "current": {"lane_name": self.lane_name}
        }
        mock.load_lane_doc.return_value = self.lane_doc
        mock.load_unit_rebind_doc.return_value = None
        mock.load_lane_leases.return_value = []
        mock.conflicting_leases.return_value = ([], [])
        mock.parse_repo_list.side_effect = lambda x: x.split(",")
        mock.build_lease.return_value = {
            "actor": self.actor, "mode": "exec", "ttl_seconds": 900
        }
        mock.mutate_lane_leases.return_value = {"status": "ok"}
        mock.find_unit_spec.return_value = {}
        mock.now_utc.return_value = "2026-04-17T00:00:00Z"
        mock.emit_lane_event.return_value = None
        return mock


class TestExecRunSequential(ExecTestBase):
    """Test sequential execution across lane repos."""

    @patch("gr2.python_cli.execops.lane_proto")
    def test_runs_command_in_each_repo(self, mock_proto):
        mock_proto.__dict__.update(self._mock_lane_proto().__dict__)
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        result = run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["echo", "hello"],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 3)
        for r in result["results"]:
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["returncode"], 0)

    @patch("gr2.python_cli.execops.lane_proto")
    def test_fail_fast_stops_on_first_failure(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        result = run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["false"],
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["results"]), 1)

    @patch("gr2.python_cli.execops.lane_proto")
    def test_collect_all_continues_after_failure(self, mock_proto):
        self.lane_doc["exec_defaults"]["fail_fast"] = False
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        result = run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["false"],
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["results"]), 3)

    @patch("gr2.python_cli.execops.lane_proto")
    def test_repo_filtering(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        result = run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["echo", "hello"],
            repos="repo-a,repo-c",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 2)
        repo_names = [r["repo"] for r in result["results"]]
        self.assertEqual(repo_names, ["repo-a", "repo-c"])

    @patch("gr2.python_cli.execops.lane_proto")
    def test_structured_results(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        result = run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["echo", "hello"],
        )
        r = result["results"][0]
        self.assertIn("repo", r)
        self.assertIn("cwd", r)
        self.assertIn("status", r)
        self.assertIn("returncode", r)
        self.assertIn("stdout", r)
        self.assertIn("stderr", r)


class TestExecRunParallel(ExecTestBase):
    """Test parallel execution mode."""

    def test_parallel_runs_all_repos(self):
        results = run_exec_parallel(
            command=["echo", "hello"],
            repo_rows=[
                {"repo": "repo-a", "cwd": str(self.workspace / "agents" / self.owner_unit / "lanes" / self.lane_name / "repos" / "repo-a")},
                {"repo": "repo-b", "cwd": str(self.workspace / "agents" / self.owner_unit / "lanes" / self.lane_name / "repos" / "repo-b")},
            ],
            max_workers=2,
        )
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.status, "ok")
            self.assertEqual(r.returncode, 0)

    def test_parallel_preserves_repo_order_in_results(self):
        results = run_exec_parallel(
            command=["echo", "hello"],
            repo_rows=[
                {"repo": "repo-c", "cwd": str(self.workspace / "agents" / self.owner_unit / "lanes" / self.lane_name / "repos" / "repo-c")},
                {"repo": "repo-a", "cwd": str(self.workspace / "agents" / self.owner_unit / "lanes" / self.lane_name / "repos" / "repo-a")},
            ],
            max_workers=2,
        )
        self.assertEqual(results[0].repo, "repo-c")
        self.assertEqual(results[1].repo, "repo-a")

    def test_parallel_handles_missing_dir(self):
        results = run_exec_parallel(
            command=["echo", "hello"],
            repo_rows=[
                {"repo": "repo-a", "cwd": str(self.workspace / "nonexistent")},
            ],
            max_workers=1,
        )
        self.assertEqual(results[0].status, "missing")


class TestExecEvents(ExecTestBase):
    """Test that exec emits proper events."""

    @patch("gr2.python_cli.execops.lane_proto")
    def test_emits_exec_started_event(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["echo", "hello"],
        )
        events = self._read_events()
        started = [e for e in events if e["type"] == "exec.started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["lane"], self.lane_name)
        self.assertIn("command", started[0])

    @patch("gr2.python_cli.execops.lane_proto")
    def test_emits_exec_completed_on_success(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["echo", "hello"],
        )
        events = self._read_events()
        completed = [e for e in events if e["type"] == "exec.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["overall_status"], "success")

    @patch("gr2.python_cli.execops.lane_proto")
    def test_emits_exec_failed_on_failure(self, mock_proto):
        for attr in dir(self._mock_lane_proto()):
            if not attr.startswith("_"):
                setattr(mock_proto, attr, getattr(self._mock_lane_proto(), attr))

        run_exec(
            self.workspace, self.owner_unit, self.lane_name,
            actor=self.actor, command=["false"],
        )
        events = self._read_events()
        failed = [e for e in events if e["type"] == "exec.failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("failed_repos", failed[0])


class TestExecResult(unittest.TestCase):
    """Test ExecResult dataclass."""

    def test_from_completed_process(self):
        r = ExecResult(
            repo="test-repo", cwd="/tmp/test", status="ok",
            returncode=0, stdout="output\n", stderr="",
        )
        self.assertEqual(r.repo, "test-repo")
        self.assertTrue(r.succeeded)

    def test_failed_result(self):
        r = ExecResult(
            repo="test-repo", cwd="/tmp/test", status="failed",
            returncode=1, stdout="", stderr="error\n",
        )
        self.assertFalse(r.succeeded)

    def test_to_dict(self):
        r = ExecResult(
            repo="test-repo", cwd="/tmp/test", status="ok",
            returncode=0, stdout="out", stderr="",
        )
        d = r.to_dict()
        self.assertEqual(d["repo"], "test-repo")
        self.assertEqual(d["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
