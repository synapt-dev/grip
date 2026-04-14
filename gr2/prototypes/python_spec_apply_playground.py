from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


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


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


def write_workspace_spec(workspace_root: Path, remote: Path) -> None:
    grip_dir = workspace_root / ".grip"
    grip_dir.mkdir(parents=True, exist_ok=True)
    (grip_dir / "workspace_spec.toml").write_text(
        "\n".join(
            [
                'workspace_name = "playground"',
                "",
                "[[repos]]",
                'name = "demo"',
                'path = "repos/demo"',
                f'url = "{remote.as_posix()}"',
                "",
                "[[units]]",
                'name = "atlas"',
                'path = "agents/atlas/home"',
                'repos = ["demo"]',
                "",
                "[[units]]",
                'name = "apollo"',
                'path = "agents/apollo/home"',
                'repos = ["demo"]',
                "",
            ]
        )
    )


def seed_bare_remote(remote: Path, *, with_hooks: bool = False) -> None:
    git("init", "--bare", str(remote))
    with tempfile.TemporaryDirectory(prefix="gr2-playground-seed-") as td:
        seed = Path(td) / "seed"
        git("clone", str(remote), str(seed))
        git("config", "user.name", "Atlas", cwd=seed)
        git("config", "user.email", "atlas@example.com", cwd=seed)
        (seed / "README.md").write_text("# demo\n")
        if with_hooks:
            hooks_dir = seed / ".gr2"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / "hooks.toml").write_text(
                "\n".join(
                    [
                        "[[files.link]]",
                        'src = "{workspace_root}/config/claude.md"',
                        'dest = "{repo_root}/CLAUDE.md"',
                        'if_exists = "overwrite"',
                        "",
                        "[[lifecycle.on_materialize]]",
                        'name = "materialize-marker"',
                        "command = \"python3 -c \\\"from pathlib import Path; Path('MATERIALIZED').write_text('ok\\\\n')\\\"\"",
                        'cwd = "{repo_root}"',
                        'when = "first_materialize"',
                        'on_failure = "block"',
                        "",
                    ]
                )
            )
        git("add", "README.md", cwd=seed)
        if with_hooks:
            git("add", ".gr2/hooks.toml", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:main", cwd=seed)


def create_demo_lane_state(workspace_root: Path) -> None:
    lane_root = workspace_root / "agents" / "atlas" / "lanes" / "feat-auth"
    (lane_root / "repos" / "demo").mkdir(parents=True, exist_ok=True)
    current_dir = workspace_root / ".grip" / "state" / "lanes" / "atlas"
    current_dir.mkdir(parents=True, exist_ok=True)
    (current_dir / "current.json").write_text(
        json.dumps(
            {
                "current": {
                    "lane_name": "feat-auth",
                    "entered_at": "2026-04-14T12:00:00Z",
                }
            },
            indent=2,
        )
    )


def scenario_missing_spec() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-spec-missing-") as td:
        workspace_root = Path(td)
        proc = pygr2("plan", str(workspace_root), check=False)
        return {
            "name": "missing-spec",
            "holds": proc.returncode != 0 and "workspace spec not found" in (proc.stderr + proc.stdout),
            "returncode": proc.returncode,
        }


def scenario_path_conflict() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-path-conflict-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        seed_bare_remote(remote)
        write_workspace_spec(workspace_root, remote)
        (workspace_root / "repos" / "demo").mkdir(parents=True, exist_ok=True)
        proc = pygr2("spec", "validate", str(workspace_root), "--json", check=False)
        payload = json.loads(proc.stdout)
        return {
            "name": "path-conflict",
            "holds": proc.returncode == 1 and any(item["code"] == "repo_path_conflict" for item in payload["issues"]),
            "issues": payload["issues"],
        }


def scenario_fresh_workspace() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-fresh-workspace-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        (workspace_root / "config").mkdir(parents=True, exist_ok=True)
        (workspace_root / "config" / "claude.md").write_text("shared claude\n")
        seed_bare_remote(remote, with_hooks=True)
        write_workspace_spec(workspace_root, remote)

        plan_before = json.loads(pygr2("plan", str(workspace_root), "--json").stdout)
        apply_payload = json.loads(pygr2("apply", str(workspace_root), "--yes", "--json").stdout)
        plan_after = json.loads(pygr2("plan", str(workspace_root), "--json").stdout)
        repo_root = workspace_root / "repos" / "demo"
        cache_root = workspace_root / ".grip" / "cache" / "repos" / "demo.git"
        alternates = repo_root / ".git" / "objects" / "info" / "alternates"

        expected_kinds = [
            "seed_repo_cache",
            "clone_repo",
            "create_unit_root",
            "write_unit_metadata",
            "create_unit_root",
            "write_unit_metadata",
        ]

        return {
            "name": "fresh-workspace",
            "holds": [item["kind"] for item in plan_before] == expected_kinds
            and apply_payload["operation_count"] == 6
            and plan_after == []
            and repo_root.joinpath(".git").exists()
            and (workspace_root / "agents" / "atlas" / "home" / "unit.toml").exists()
            and (workspace_root / "agents" / "apollo" / "home" / "unit.toml").exists()
            and cache_root.exists()
            and alternates.exists()
            and repo_root.joinpath("CLAUDE.md").exists()
            and repo_root.joinpath("MATERIALIZED").exists(),
            "plan_before": plan_before,
            "apply_payload": apply_payload,
            "cache_root_exists": cache_root.exists(),
            "alternates_exists": alternates.exists(),
            "claude_link_exists": repo_root.joinpath("CLAUDE.md").exists(),
            "materialize_marker_exists": repo_root.joinpath("MATERIALIZED").exists(),
        }


def scenario_dirty_shared_repo_preserved() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-dirty-shared-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        seed_bare_remote(remote)
        write_workspace_spec(workspace_root, remote)
        pygr2("apply", str(workspace_root), "--yes")
        repo_root = workspace_root / "repos" / "demo"
        (repo_root / "LOCAL.txt").write_text("dirty\n")
        plan_payload = json.loads(pygr2("plan", str(workspace_root), "--json").stdout)
        apply_payload = json.loads(pygr2("apply", str(workspace_root), "--json").stdout)
        preserved = (repo_root / "LOCAL.txt").exists()
        return {
            "name": "dirty-shared-repo-preserved",
            "holds": plan_payload == [] and apply_payload["operation_count"] == 0 and preserved,
            "plan_payload": plan_payload,
            "apply_payload": apply_payload,
        }


def scenario_lane_state_untouched() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-lane-state-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        seed_bare_remote(remote)
        write_workspace_spec(workspace_root, remote)
        create_demo_lane_state(workspace_root)
        before = (workspace_root / ".grip" / "state" / "lanes" / "atlas" / "current.json").read_text()
        pygr2("apply", str(workspace_root), "--yes")
        after = (workspace_root / ".grip" / "state" / "lanes" / "atlas" / "current.json").read_text()
        lane_checkout_still_absent = not (workspace_root / "agents" / "atlas" / "lanes" / "feat-auth" / "repos" / "demo" / ".git").exists()
        return {
            "name": "lane-state-untouched",
            "holds": before == after and lane_checkout_still_absent,
        }


def scenario_invalid_repo_hooks() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-invalid-hooks-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        seed_bare_remote(remote)
        write_workspace_spec(workspace_root, remote)
        pygr2("apply", str(workspace_root), "--yes")
        hooks_dir = workspace_root / "repos" / "demo" / ".gr2"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.toml").write_text(
            "\n".join(
                [
                    "[[lifecycle.on_enter]]",
                    'name = "broken"',
                    'command = "true"',
                    'when = "not-a-real-when"',
                    "",
                ]
            )
        )
        proc = pygr2("spec", "validate", str(workspace_root), "--json", check=False)
        payload = json.loads(proc.stdout)
        return {
            "name": "invalid-repo-hooks",
            "holds": proc.returncode == 1 and any(item["code"] == "invalid_repo_hooks" for item in payload["issues"]),
            "issues": payload["issues"],
        }


SCENARIOS = [
    scenario_missing_spec,
    scenario_path_conflict,
    scenario_fresh_workspace,
    scenario_dirty_shared_repo_preserved,
    scenario_lane_state_untouched,
    scenario_invalid_repo_hooks,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Playground verification for Python gr2 spec/plan/apply.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [scenario() for scenario in SCENARIOS]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {"verdict": verdict, "results": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"python spec/apply playground verdict: {verdict}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
