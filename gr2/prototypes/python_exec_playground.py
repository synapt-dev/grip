from __future__ import annotations

import argparse
import json
import os
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


def seed_remote(remote: Path) -> None:
    git("init", "--bare", str(remote))
    with tempfile.TemporaryDirectory(prefix="gr2-exec-seed-") as td:
        seed = Path(td) / "seed"
        git("clone", str(remote), str(seed))
        git("config", "user.name", "Atlas", cwd=seed)
        git("config", "user.email", "atlas@example.com", cwd=seed)
        (seed / "README.md").write_text("# demo\n")
        git("add", "README.md", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:main", cwd=seed)
    git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")


def write_spec(workspace_root: Path, remote: Path) -> None:
    grip_dir = workspace_root / ".grip"
    grip_dir.mkdir(parents=True, exist_ok=True)
    (grip_dir / "workspace_spec.toml").write_text(
        "\n".join(
            [
                'workspace_name = "exec-playground"',
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
            ]
        )
    )


def base_workspace() -> Path:
    workspace_root = Path(tempfile.mkdtemp(prefix="gr2-exec-playground-"))
    remote = workspace_root / "demo-remote.git"
    seed_remote(remote)
    write_spec(workspace_root, remote)
    pygr2("apply", str(workspace_root), "--yes")
    pygr2(
        "lane",
        "create",
        str(workspace_root),
        "atlas",
        "feat-auth",
        "--repos",
        "demo",
        "--branch",
        "feat/auth",
    )
    pygr2("lane", "enter", str(workspace_root), "atlas", "feat-auth", "--actor", "agent:atlas")
    return workspace_root


def scenario_exec_status_ready() -> dict[str, object]:
    workspace_root = base_workspace()
    payload = json.loads(pygr2("exec", "status", str(workspace_root), "atlas", "--json").stdout)
    row = payload["rows"][0]
    return {
        "name": "exec-status-ready",
        "holds": payload["status"] == "ready"
        and payload["lane"] == "feat-auth"
        and row["repo"] == "demo"
        and row["exists"] is True
        and row["cwd"].endswith("/agents/atlas/lanes/feat-auth/repos/demo"),
        "payload": payload,
    }


def scenario_exec_run_success() -> dict[str, object]:
    workspace_root = base_workspace()
    payload = json.loads(
        pygr2(
            "exec",
            "run",
            str(workspace_root),
            "atlas",
            "python3",
            "-c",
            "from pathlib import Path; Path('EXEC_OK').write_text('ok\\n')",
            "--actor",
            "agent:atlas",
            "--json",
        ).stdout
    )
    repo_root = workspace_root / "agents" / "atlas" / "lanes" / "feat-auth" / "repos" / "demo"
    leases_path = workspace_root / "agents" / "atlas" / "lanes" / "feat-auth" / "leases.json"
    leases = json.loads(leases_path.read_text()) if leases_path.exists() else []
    return {
        "name": "exec-run-success",
        "holds": payload["status"] == "success"
        and payload["results"][0]["status"] == "ok"
        and repo_root.joinpath("EXEC_OK").exists()
        and leases == [],
        "payload": payload,
    }


def scenario_exec_blocked_by_edit() -> dict[str, object]:
    workspace_root = base_workspace()
    pygr2(
        "lane",
        "lease",
        "acquire",
        str(workspace_root),
        "atlas",
        "feat-auth",
        "--actor",
        "human:layne",
        "--mode",
        "edit",
    )
    proc = pygr2(
        "exec",
        "run",
        str(workspace_root),
        "atlas",
        "pwd",
        "--actor",
        "agent:atlas",
        "--json",
        check=False,
    )
    payload = json.loads(proc.stdout)
    return {
        "name": "exec-blocked-by-edit",
        "holds": proc.returncode == 1
        and payload["status"] == "blocked"
        and payload["reason"] == "conflicting-active-lease",
        "payload": payload,
    }


SCENARIOS = [
    scenario_exec_status_ready,
    scenario_exec_run_success,
    scenario_exec_blocked_by_edit,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Playground verification for Python gr2 exec status/run.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [scenario() for scenario in SCENARIOS]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {"verdict": verdict, "results": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"python exec playground verdict: {verdict}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
