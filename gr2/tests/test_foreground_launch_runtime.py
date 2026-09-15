"""Foreground launch runtime (interactive half of the launch seam).

One named agent runs in THIS terminal with its configured cwd, environment
and argv; a fleet with no multiplexer is refused. The probes here cover what
the happy path cannot see: the fleet refusal, the constructed environment
(not inherited), values never reaching the filesystem, and the exit code as
the evidence of a run that happened.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from gr2.python_cli.launch_exec import (
    ForegroundProcessRuntime,
    LaunchEntry,
    LaunchExecutionError,
    launch_team,
)

SECRET = "FG-AGENT-VALUE-THAT-MUST-NEVER-TOUCH-DISK"
AMBIENT = "FG-AMBIENT-UNDECLARED-KEY"


def _entry(tmp: Path, argv_suffix: str) -> LaunchEntry:
    return LaunchEntry.from_mapping(
        {
            "unit_key": "agent-a",
            "workdir": "work",
            "argv": [sys.executable, "-c", argv_suffix],
            "env_allowlist_keys": ["FG_DECLARED"],
        }
    )


class TestForegroundLaunchRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="fg-launch-"))
        (self._tmp / "work").mkdir()
        os.environ[AMBIENT] = "ambient-must-not-cross"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp)
        os.environ.pop(AMBIENT, None)

    def test_fleet_without_multiplexer_is_refused(self) -> None:
        entries = [
            _entry(self._tmp, "pass"),
            LaunchEntry.from_mapping(
                {
                    "unit_key": "agent-b",
                    "workdir": "work",
                    "argv": [sys.executable, "-c", "pass"],
                    "env_allowlist_keys": [],
                }
            ),
        ]
        with self.assertRaises(LaunchExecutionError) as ctx:
            ForegroundProcessRuntime().launch_team(
                entries,
                workspace_root=self._tmp,
                env_values_by_unit={e.unit_key: {} for e in entries},
                settle_seconds=0.01,
            )
        self.assertIn("ONE agent", str(ctx.exception))

    def test_foreground_run_executes_with_configured_cwd_and_env(self) -> None:
        marker = self._tmp / "fg-evidence.json"
        script = (
            "import json, os\n"
            "json.dump({'declared': os.environ.get('FG_DECLARED'), "
            "'ambient': os.environ.get('FG_AMBIENT_UNDECLARED_KEY'), "
            "'has_path': 'PATH' in os.environ}, open('fg-evidence.json', 'w'))\n"
        )
        evidence = launch_team(
            [_entry(self._tmp, script)],
            workspace_root=self._tmp,
            env_values_by_unit={"agent-a": {"FG_DECLARED": SECRET}},
            runtime=ForegroundProcessRuntime(),
        )[0]
        self.assertEqual(evidence["mode"], "foreground")
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["unit_key"], "agent-a")
        self.assertEqual(evidence["env_keys"], ["FG_DECLARED"])
        observed = json.loads((self._tmp / "work" / "fg-evidence.json").read_text())
        self.assertEqual(observed["declared"], SECRET)
        self.assertIsNone(observed["ambient"])
        self.assertTrue(observed["has_path"])

    def test_no_environment_value_reaches_the_filesystem(self) -> None:
        script = "import sys\nsys.exit(0)\n"
        launch_team(
            [_entry(self._tmp, script)],
            workspace_root=self._tmp,
            env_values_by_unit={"agent-a": {"FG_DECLARED": SECRET}},
            runtime=ForegroundProcessRuntime(),
        )
        for path in self._tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_missing_workdir_is_refused(self) -> None:
        entry = LaunchEntry.from_mapping(
            {
                "unit_key": "agent-a",
                "workdir": "ghost-workdir",
                "argv": [sys.executable, "-c", "pass"],
                "env_allowlist_keys": ["FG_DECLARED"],
            }
        )
        with self.assertRaises(LaunchExecutionError) as ctx:
            ForegroundProcessRuntime().launch_team(
                [entry],
                workspace_root=self._tmp,
                env_values_by_unit={"agent-a": {"FG_DECLARED": "x"}},
                settle_seconds=0.01,
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_missing_binary_is_refused(self) -> None:
        entry = LaunchEntry.from_mapping(
            {
                "unit_key": "agent-a",
                "workdir": "work",
                "argv": ["definitely-not-a-real-binary-fg"],
                "env_allowlist_keys": [],
            }
        )
        with self.assertRaises(LaunchExecutionError) as ctx:
            ForegroundProcessRuntime().launch_team(
                [entry],
                workspace_root=self._tmp,
                env_values_by_unit={"agent-a": {}},
                settle_seconds=0.01,
            )
        self.assertIn("not found on PATH", str(ctx.exception))

    def test_exit_code_is_the_evidence_of_an_instant_exit(self) -> None:
        script = "import sys\nsys.exit(7)\n"
        evidence = ForegroundProcessRuntime().launch_team(
            [_entry(self._tmp, script)],
            workspace_root=self._tmp,
            env_values_by_unit={"agent-a": {"FG_DECLARED": "x"}},
            settle_seconds=0.01,
        )[0]
        self.assertEqual(evidence["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()