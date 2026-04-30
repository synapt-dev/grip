from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import tomllib
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


def seed_remote_with_review_branch(remote: Path, branch_name: str) -> None:
    git("init", "--bare", str(remote))
    with tempfile.TemporaryDirectory(prefix="gr2-review-seed-") as td:
        seed = Path(td) / "seed"
        git("clone", str(remote), str(seed))
        git("config", "user.name", "Atlas", cwd=seed)
        git("config", "user.email", "atlas@example.com", cwd=seed)
        (seed / "README.md").write_text("# demo\n")
        git("add", "README.md", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:main", cwd=seed)
        git("checkout", "-b", branch_name, cwd=seed)
        (seed / "REVIEW.txt").write_text("review branch\n")
        git("add", "REVIEW.txt", cwd=seed)
        git("commit", "-m", "review branch", cwd=seed)
        git("push", "origin", f"HEAD:{branch_name}", cwd=seed)
    git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")


def advance_review_branch(remote: Path, branch_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="gr2-review-advance-") as td:
        seed = Path(td) / "seed"
        git("clone", str(remote), str(seed))
        git("config", "user.name", "Atlas", cwd=seed)
        git("config", "user.email", "atlas@example.com", cwd=seed)
        git("checkout", branch_name, cwd=seed)
        (seed / "REVIEW_2.txt").write_text("updated review branch\n")
        git("add", "REVIEW_2.txt", cwd=seed)
        git("commit", "-m", "review branch update", cwd=seed)
        git("push", "origin", f"HEAD:{branch_name}", cwd=seed)


def remove_lane_checkout(shared_repo_root: Path, lane_repo_root: Path) -> None:
    git("-C", str(shared_repo_root), "worktree", "remove", "--force", str(lane_repo_root))


def write_spec(workspace_root: Path, remote: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True, exist_ok=True)
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(
        "\n".join(
            [
                'workspace_name = "review-playground"',
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


def scenario_review_checkout_and_enter() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-review-playground-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        review_branch = "review/demo-42"
        seed_remote_with_review_branch(remote, review_branch)
        write_spec(workspace_root, remote)
        pygr2("apply", str(workspace_root), "--yes")

        payload = json.loads(
            pygr2(
                "review",
                "checkout-pr",
                str(workspace_root),
                "atlas",
                "demo",
                "42",
                "--branch",
                review_branch,
                "--enter",
                "--actor",
                "agent:atlas",
                "--json",
            ).stdout
        )

        lane_repo_root = Path(payload["lane_repo_root"])
        lane_file = workspace_root / "agents" / "atlas" / "lanes" / "review-42" / "lane.toml"
        current_file = workspace_root / ".grip" / "state" / "current_lane" / "atlas.json"
        current = json.loads(current_file.read_text())
        lane_doc = tomllib.loads(lane_file.read_text())
        branch = git("branch", "--show-current", cwd=lane_repo_root).stdout.strip()

        return {
            "name": "review-checkout-and-enter",
            "holds": payload["lane_name"] == "review-42"
            and payload["branch"] == review_branch
            and payload["entered"] is True
            and lane_repo_root.joinpath(".git").exists()
            and lane_repo_root.joinpath("REVIEW.txt").exists()
            and branch == review_branch
            and current["current"]["lane_name"] == "review-42"
            and lane_doc["lane_type"] == "review"
            and lane_doc["pr_associations"][0]["ref"] == "demo#42",
            "payload": payload,
        }


def scenario_missing_shared_repo_blocks() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-review-missing-shared-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        review_branch = "review/demo-42"
        seed_remote_with_review_branch(remote, review_branch)
        write_spec(workspace_root, remote)
        proc = pygr2(
            "review",
            "checkout-pr",
            str(workspace_root),
            "atlas",
            "demo",
            "42",
            "--branch",
            review_branch,
            "--json",
            check=False,
        )
        return {
            "name": "missing-shared-repo-blocks",
            "holds": proc.returncode != 0 and "shared repo missing for review checkout" in (proc.stderr + proc.stdout),
        }


def scenario_existing_local_branch_refetches() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-review-refetch-") as td:
        workspace_root = Path(td)
        remote = workspace_root / "demo-remote.git"
        review_branch = "review/demo-42"
        seed_remote_with_review_branch(remote, review_branch)
        write_spec(workspace_root, remote)
        pygr2("apply", str(workspace_root), "--yes")

        pygr2(
            "review",
            "checkout-pr",
            str(workspace_root),
            "atlas",
            "demo",
            "42",
            "--lane",
            "review-42-old",
            "--branch",
            review_branch,
            "--json",
        )

        shared_repo_root = workspace_root / "repos" / "demo"
        old_lane_repo_root = workspace_root / "agents" / "atlas" / "lanes" / "review-42-old" / "repos" / "demo"
        remove_lane_checkout(shared_repo_root, old_lane_repo_root)

        advance_review_branch(remote, review_branch)

        payload = json.loads(
            pygr2(
                "review",
                "checkout-pr",
                str(workspace_root),
                "atlas",
                "demo",
                "42",
                "--lane",
                "review-42-new",
                "--branch",
                review_branch,
                "--json",
            ).stdout
        )

        lane_repo_root = Path(payload["lane_repo_root"])
        return {
            "name": "existing-local-branch-refetches",
            "holds": lane_repo_root.joinpath("REVIEW_2.txt").exists(),
            "payload": payload,
        }


SCENARIOS = [
    scenario_review_checkout_and_enter,
    scenario_missing_shared_repo_blocks,
    scenario_existing_local_branch_refetches,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Playground verification for Python gr2 review checkout-pr.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [scenario() for scenario in SCENARIOS]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {"verdict": verdict, "results": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"python review checkout verdict: {verdict}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
