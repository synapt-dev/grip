#!/usr/bin/env python3
"""Concurrent lease stress harness with before/after locking results."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run concurrent lease stress before and after file locking"
    )
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lane_proto(root: Path) -> Path:
    return root / "gr2" / "prototypes" / "lane_workspace_prototype.py"


def run(argv: list[str], *, env: dict | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, text=True, capture_output=capture, env=env)


def init_workspace(workspace_root: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True, exist_ok=True)
    (workspace_root / "agents").mkdir(exist_ok=True)
    spec = """schema_version = 1
workspace_name = "concurrent-lease-stress"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://example.invalid/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
agent_id = "atlas-agent"
repos = ["app"]
"""
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(spec)


def create_lane(root: Path, workspace_root: Path) -> None:
    run(
        [
            "python3",
            str(lane_proto(root)),
            "create-lane",
            str(workspace_root),
            "atlas",
            "feat-race",
            "--repos",
            "app",
            "--branch",
            "feat/race",
        ],
        capture=True,
    )


def worker(workspace_root: str, actor: str, queue, disable_locking: bool) -> None:
    root = repo_root()
    env = os.environ.copy()
    if disable_locking:
        env["GR2_DISABLE_LEASE_LOCKING"] = "1"
        env["GR2_LEASE_TEST_DELAY"] = "0.02"
    proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "acquire-lane-lease",
            workspace_root,
            "atlas",
            "feat-race",
            "--actor",
            actor,
            "--mode",
            "edit",
            "--ttl-seconds",
            "900",
        ],
        env=env,
        check=False,
        capture=True,
    )
    queue.put(
        {
            "actor": actor,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    )


def read_leases(workspace_root: Path) -> tuple[bool, list[dict] | None]:
    path = workspace_root / "agents" / "atlas" / "lanes" / "feat-race" / "leases.json"
    if not path.exists():
        return True, []
    try:
        return True, json.loads(path.read_text())
    except json.JSONDecodeError:
        return False, None


def release_all(root: Path, workspace_root: Path, disable_locking: bool) -> None:
    env = os.environ.copy()
    if disable_locking:
        env["GR2_DISABLE_LEASE_LOCKING"] = "1"
    for actor in ("worker:a", "worker:b"):
        run(
            [
                "python3",
                str(lane_proto(root)),
                "release-lane-lease",
                str(workspace_root),
                "atlas",
                "feat-race",
                "--actor",
                actor,
            ],
            env=env,
            check=False,
            capture=True,
        )


def run_phase(disable_locking: bool, rounds: int) -> dict:
    root = repo_root()
    with tempfile.TemporaryDirectory(prefix="gr2-concurrent-lease-") as tmp:
        workspace_root = Path(tmp)
        init_workspace(workspace_root)
        create_lane(root, workspace_root)

        corruption_count = 0
        both_succeeded_count = 0
        unexpected_lease_count = 0

        for _ in range(rounds):
            queue = multiprocessing.Queue()
            p1 = multiprocessing.Process(
                target=worker,
                args=(str(workspace_root), "worker:a", queue, disable_locking),
            )
            p2 = multiprocessing.Process(
                target=worker,
                args=(str(workspace_root), "worker:b", queue, disable_locking),
            )
            p1.start()
            p2.start()
            p1.join()
            p2.join()

            results = [queue.get(), queue.get()]
            success_count = sum(1 for item in results if item["returncode"] == 0)
            if success_count == 2:
                both_succeeded_count += 1

            valid_json, leases = read_leases(workspace_root)
            if not valid_json:
                corruption_count += 1
            else:
                expected = 1 if success_count >= 1 else 0
                if len(leases or []) != expected:
                    unexpected_lease_count += 1

            release_all(root, workspace_root, disable_locking)

        return {
            "locking": "disabled" if disable_locking else "enabled",
            "rounds": rounds,
            "corruption_count": corruption_count,
            "both_succeeded_count": both_succeeded_count,
            "unexpected_lease_count": unexpected_lease_count,
        }


def main() -> int:
    args = parse_args()
    before = run_phase(disable_locking=True, rounds=args.rounds)
    after = run_phase(disable_locking=False, rounds=args.rounds)
    payload = {"before_locking": before, "after_locking": after}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("gr2 concurrent lease stress")
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
