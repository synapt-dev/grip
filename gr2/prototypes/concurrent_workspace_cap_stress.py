#!/usr/bin/env python3
"""Repeated stress harness for the workspace-wide edit-lease cap."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lane_proto(root: Path) -> Path:
    return root / "gr2" / "prototypes" / "lane_workspace_prototype.py"


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, env=env, check=False)


def _init_workspace(workspace_root: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True)
    (workspace_root / "agents").mkdir()
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(
        """schema_version = 1
workspace_name = "workspace-cap-stress"

[cache]
root = ".grip/cache"

[workspace_constraints]
max_concurrent_edit_leases_global = 1

[[repos]]
name = "app"
path = "repos/app"
url = "https://example.invalid/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["app"]

[[units]]
name = "apollo"
path = "agents/apollo"
repos = ["app"]
""",
        encoding="utf-8",
    )
    root = repo_root()
    for unit, lane in (("atlas", "lane-a"), ("apollo", "lane-b")):
        result = _run(
            [
                sys.executable,
                str(lane_proto(root)),
                "create-lane",
                str(workspace_root),
                unit,
                lane,
                "--repos",
                "app",
                "--branch",
                f"feat/{lane}",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)


def _acquire_worker(
    workspace_root: str,
    unit: str,
    lane: str,
    actor: str,
    start: object,
    queue: object,
    disable_locking: bool,
) -> None:
    if not start.wait(timeout=10):
        queue.put({"returncode": 99, "error": "start gate timed out"})
        return
    env = os.environ.copy()
    env["GR2_WORKSPACE_CAP_TEST_DELAY"] = "0.04"
    if disable_locking:
        env["GR2_DISABLE_LEASE_LOCKING"] = "1"
        env["GR2_LEASE_TEST_DELAY"] = "0.04"
    result = _run(
        [
            sys.executable,
            str(lane_proto(repo_root())),
            "acquire-lane-lease",
            workspace_root,
            unit,
            lane,
            "--actor",
            actor,
            "--mode",
            "edit",
            "--ttl-seconds",
            "900",
        ],
        env=env,
    )
    queue.put(
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )


def _active_edit_count(workspace_root: Path) -> int:
    count = 0
    for path in workspace_root.glob(".grip/state/lanes/*/*/leases.json"):
        for lease in json.loads(path.read_text()):
            count += lease.get("mode") == "edit"
    return count


def _run_round(*, disable_locking: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-workspace-cap-") as tmp:
        workspace_root = Path(tmp)
        _init_workspace(workspace_root)
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_acquire_worker,
                args=(
                    str(workspace_root),
                    unit,
                    lane,
                    f"agent:{unit}",
                    start,
                    queue,
                    disable_locking,
                ),
            )
            for unit, lane in (("atlas", "lane-a"), ("apollo", "lane-b"))
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                raise RuntimeError("workspace-cap worker hung")
        outcomes = [queue.get(timeout=2) for _ in processes]
        active_count = _active_edit_count(workspace_root)
        return {
            "worker_failures": sum(row["returncode"] not in (0, 1) for row in outcomes),
            "active_edit_count": active_count,
            "cap_violation": active_count > 1,
        }


def run_phase(*, rounds: int, disable_locking: bool) -> dict[str, object]:
    results = [_run_round(disable_locking=disable_locking) for _ in range(rounds)]
    return {
        "locking": "disabled" if disable_locking else "enabled",
        "rounds": rounds,
        "cap_violation_rounds": sum(bool(row["cap_violation"]) for row in results),
        "worker_failure_rounds": sum(bool(row["worker_failures"]) for row in results),
    }


def sequential_control() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-workspace-cap-sequential-") as tmp:
        workspace_root = Path(tmp)
        _init_workspace(workspace_root)
        outcomes = []
        for unit, lane in (("atlas", "lane-a"), ("apollo", "lane-b")):
            outcomes.append(
                _run(
                    [
                        sys.executable,
                        str(lane_proto(repo_root())),
                        "acquire-lane-lease",
                        str(workspace_root),
                        unit,
                        lane,
                        "--actor",
                        f"agent:{unit}",
                        "--mode",
                        "edit",
                    ]
                ).returncode
            )
        active_count = _active_edit_count(workspace_root)
        return {
            "returncodes": outcomes,
            "active_edit_count": active_count,
            "cap_violation": active_count > 1,
        }


def main() -> int:
    args = parse_args()
    payload = {
        "sequential_control": sequential_control(),
        "before_workspace_lock": run_phase(rounds=args.rounds, disable_locking=True),
        "after_workspace_lock": run_phase(rounds=args.rounds, disable_locking=False),
    }
    print(json.dumps(payload, indent=2))
    after = payload["after_workspace_lock"]
    return int(
        payload["sequential_control"]["cap_violation"]
        or after["cap_violation_rounds"] != 0
        or after["worker_failure_rounds"] != 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
