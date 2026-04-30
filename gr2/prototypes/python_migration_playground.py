from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def pygr2(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["python3", "-m", "gr2.python_cli", *args], cwd=cwd, check=check)


def write_gr1_workspace(workspace_root: Path) -> None:
    gitgrip = workspace_root / ".gitgrip"
    (gitgrip / "spaces" / "main").mkdir(parents=True, exist_ok=True)
    (gitgrip / "spaces" / "main" / "gripspace.yml").write_text(
        "\n".join(
            [
                "version: 2",
                "manifest:",
                "  url: git@github.com:synapt-dev/synapt-gripspace.git",
                "repos:",
                "  grip:",
                "    url: git@github.com:synapt-dev/grip.git",
                "    path: ./gitgrip",
                "    revision: main",
                "  synapt:",
                "    url: git@github.com:synapt-dev/synapt.git",
                "    path: ./synapt",
                "    revision: main",
                "  mem0:",
                "    url: https://github.com/mem0ai/mem0.git",
                "    path: reference/mem0",
                "    default_branch: main",
                "    reference: true",
                "",
            ]
        )
    )
    (gitgrip / "agents.toml").write_text(
        "\n".join(
            [
                "[agents.atlas]",
                'worktree = "main"',
                'channel = "dev"',
                "",
                "[agents.apollo]",
                'worktree = "main"',
                'channel = "dev"',
                "",
            ]
        )
    )
    (gitgrip / "state.json").write_text(json.dumps({"branchToPr": {"feat/auth": 123}}, indent=2))
    (gitgrip / "sync-state.json").write_text(json.dumps({"timestamp": "2026-04-14T12:00:00Z"}, indent=2))
    (gitgrip / "griptrees.json").write_text(json.dumps({"griptrees": {"review-pr1": {"path": "/tmp/review-pr1", "branch": "review-pr1"}}}, indent=2))


def scenario_detect_and_migrate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-migration-") as td:
        workspace_root = Path(td)
        write_gr1_workspace(workspace_root)
        manifest_before = (workspace_root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()

        detect = json.loads(pygr2("workspace", "detect-gr1", str(workspace_root), "--json").stdout)
        migrate = json.loads(pygr2("workspace", "migrate-gr1", str(workspace_root), "--json").stdout)
        spec_text = (workspace_root / ".grip" / "workspace_spec.toml").read_text()
        manifest_after = (workspace_root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()

        return {
            "name": "detect-and-migrate-gr1",
            "holds": detect["detected"] is True
            and detect["repo_count"] == 3
            and set(detect["agents"]) == {"apollo", "atlas"}
            and "gitgrip" in spec_text
            and 'name = "atlas"' in spec_text
            and 'name = "apollo"' in spec_text
            and (workspace_root / ".grip" / "migrations" / "gr1" / "state.json").exists()
            and (workspace_root / ".grip" / "migrations" / "gr1" / "sync-state.json").exists()
            and (workspace_root / ".grip" / "migrations" / "gr1" / "griptrees.json").exists()
            and manifest_before == manifest_after,
            "detect": detect,
            "migrate": migrate,
        }


def scenario_existing_gr2_blocks_without_force() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-migration-force-") as td:
        workspace_root = Path(td)
        write_gr1_workspace(workspace_root)
        grip_dir = workspace_root / ".grip"
        grip_dir.mkdir(parents=True, exist_ok=True)
        (grip_dir / "workspace_spec.toml").write_text('workspace_name = "existing"\n')
        blocked = pygr2("workspace", "migrate-gr1", str(workspace_root), "--json", check=False)
        forced = pygr2("workspace", "migrate-gr1", str(workspace_root), "--json", "--force")
        forced_payload = json.loads(forced.stdout)
        return {
            "name": "migrate-force-guard",
            "holds": blocked.returncode != 0
            and "refusing to overwrite existing gr2 workspace spec" in (blocked.stderr + blocked.stdout)
            and forced_payload["unit_count"] == 2,
        }


SCENARIOS = [
    scenario_detect_and_migrate,
    scenario_existing_gr2_blocks_without_force,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Playground verification for gr1 -> Python gr2 migration.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [scenario() for scenario in SCENARIOS]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {"verdict": verdict, "results": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"python migration playground verdict: {verdict}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
