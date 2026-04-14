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
    with tempfile.TemporaryDirectory(prefix="gr2-hooks-seed-") as td:
        seed = Path(td) / "seed"
        git("clone", str(remote), str(seed))
        git("config", "user.name", "Atlas", cwd=seed)
        git("config", "user.email", "atlas@example.com", cwd=seed)
        (seed / "README.md").write_text("# demo\n")
        hooks_dir = seed / ".gr2"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.toml").write_text(
            "\n".join(
                [
                    "[[files.copy]]",
                    'src = "{workspace_root}/shared.txt"',
                    'dest = "{repo_root}/COPY_SKIP.txt"',
                    'if_exists = "skip"',
                    "",
                    "[[files.copy]]",
                    'src = "{workspace_root}/shared.txt"',
                    'dest = "{repo_root}/COPY_OVERWRITE.txt"',
                    'if_exists = "overwrite"',
                    "",
                    "[[lifecycle.on_enter]]",
                    'name = "manual-write"',
                    "command = \"python3 -c \\\"from pathlib import Path; Path('MANUAL.txt').write_text('manual\\\\n')\\\"\"",
                    'cwd = "{repo_root}"',
                    'when = "manual"',
                    'on_failure = "warn"',
                    "",
                    "[[lifecycle.on_enter]]",
                    'name = "warn-fail"',
                    'command = "python3 -c \\"import sys; sys.exit(3)\\""',
                    'cwd = "{repo_root}"',
                    'when = "always"',
                    'on_failure = "warn"',
                    "",
                    "[[lifecycle.on_enter]]",
                    'name = "skip-fail"',
                    'command = "python3 -c \\"import sys; sys.exit(5)\\""',
                    'cwd = "{repo_root}"',
                    'when = "always"',
                    'on_failure = "skip"',
                    "",
                    "[[lifecycle.on_exit]]",
                    'name = "block-fail"',
                    'command = "python3 -c \\"import sys; sys.exit(7)\\""',
                    'cwd = "{repo_root}"',
                    'when = "always"',
                    'on_failure = "block"',
                    "",
                ]
            )
        )
        git("add", "README.md", ".gr2/hooks.toml", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:main", cwd=seed)
    git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")


def write_spec(workspace_root: Path, remote: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True, exist_ok=True)
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(
        "\n".join(
            [
                'workspace_name = "hooks-playground"',
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
    workspace_root = Path(tempfile.mkdtemp(prefix="gr2-hooks-playground-"))
    (workspace_root / "shared.txt").write_text("shared\n")
    remote = workspace_root / "demo-remote.git"
    seed_remote(remote)
    write_spec(workspace_root, remote)
    pygr2("apply", str(workspace_root), "--yes")
    repo_root = workspace_root / "repos" / "demo"
    repo_root.joinpath("COPY_SKIP.txt").write_text("original\n")
    repo_root.joinpath("COPY_OVERWRITE.txt").write_text("old\n")
    pygr2(
        "lane",
        "create",
        str(workspace_root),
        "atlas",
        "feat-hooks",
        "--repos",
        "demo",
        "--branch",
        "feat/hooks",
    )
    return workspace_root


def scenario_manual_hook_flag() -> dict[str, object]:
    workspace_root = base_workspace()
    repo_root = workspace_root / "agents" / "atlas" / "lanes" / "feat-hooks" / "repos" / "demo"
    pygr2("lane", "enter", str(workspace_root), "atlas", "feat-hooks", "--actor", "agent:atlas")
    without_manual = not repo_root.joinpath("MANUAL.txt").exists()
    with_manual = json.loads(
        pygr2(
            "repo",
            "hook-run",
            str(workspace_root),
            str(repo_root),
            "on_enter",
            "--manual",
            "--json",
        ).stdout
    )
    return {
        "name": "manual-hook-flag",
        "holds": without_manual
        and repo_root.joinpath("MANUAL.txt").exists()
        and any(item["name"] == "manual-write" and item["status"] == "applied" for item in with_manual["results"]),
        "payload": with_manual,
    }


def scenario_warn_skip_and_block() -> dict[str, object]:
    workspace_root = base_workspace()
    repo_root = workspace_root / "agents" / "atlas" / "lanes" / "feat-hooks" / "repos" / "demo"
    warn_skip = json.loads(
        pygr2(
            "repo",
            "hook-run",
            str(workspace_root),
            str(repo_root),
            "on_enter",
            "--json",
        ).stdout
    )
    block = pygr2(
        "repo",
        "hook-run",
        str(workspace_root),
        str(repo_root),
        "on_exit",
        "--json",
        check=False,
    )
    return {
        "name": "warn-skip-block",
        "holds": any(item["name"] == "warn-fail" and item["status"] == "warned" for item in warn_skip["results"])
        and any(item["name"] == "skip-fail" and item["status"] == "skipped" for item in warn_skip["results"])
        and block.returncode != 0
        and '"on_failure": "block"' in (block.stderr + block.stdout),
    }


def scenario_projection_if_exists() -> dict[str, object]:
    workspace_root = base_workspace()
    repo_root = workspace_root / "repos" / "demo"
    payload = json.loads(
        pygr2(
            "repo",
            "projection-run",
            str(workspace_root),
            str(repo_root),
            "--json",
        ).stdout
    )
    return {
        "name": "projection-if-exists",
        "holds": repo_root.joinpath("COPY_SKIP.txt").read_text() == "original\n"
        and repo_root.joinpath("COPY_OVERWRITE.txt").read_text() == "shared\n"
        and any(item["status"] == "skipped" for item in payload["results"])
        and any(item["status"] == "applied" for item in payload["results"]),
    }


SCENARIOS = [
    scenario_manual_hook_flag,
    scenario_warn_skip_and_block,
    scenario_projection_if_exists,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Playground verification for Python gr2 hook runtime semantics.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [scenario() for scenario in SCENARIOS]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {"verdict": verdict, "results": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"python hook runtime verdict: {verdict}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
