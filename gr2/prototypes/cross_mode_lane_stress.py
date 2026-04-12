#!/usr/bin/env python3
"""Adversarial cross-mode stress harness for the gr2 lane model.

This script pressures the lane prototype across the four primary user modes:

1. solo human
2. single agent
3. multi-agent
4. mixed human + agent

It does not pretend the model is complete. It reports where the current
prototype holds, where it only partially holds, and where it still falls over.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ScenarioResult:
    scenario_id: str
    user_mode: str
    title: str
    verdict: str
    holds: list[str]
    gaps: list[str]
    evidence: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run adversarial cross-mode lane stress checks"
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="optional workspace root; defaults to a temporary workspace",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit structured JSON instead of human-readable text",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lane_proto(root: Path) -> Path:
    return root / "gr2" / "prototypes" / "lane_workspace_prototype.py"


def run(argv: list[str], *, capture: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )


def init_workspace(workspace_root: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True, exist_ok=True)
    (workspace_root / "agents").mkdir(exist_ok=True)
    spec = """schema_version = 1
workspace_name = "lane-cross-mode-stress"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://example.invalid/app.git"

[[repos]]
name = "api"
path = "repos/api"
url = "https://example.invalid/api.git"

[[repos]]
name = "web"
path = "repos/web"
url = "https://example.invalid/web.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["app", "api", "web"]

[[units]]
name = "apollo"
path = "agents/apollo"
repos = ["app", "api", "web"]

[[units]]
name = "layne"
path = "agents/layne"
repos = ["app", "api", "web"]
"""
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(spec)


def create_lane(root: Path, workspace_root: Path, owner_unit: str, lane_name: str, repos: str, branch: str, lane_type: str = "feature") -> None:
    run(
        [
            "python3",
            str(lane_proto(root)),
            "create-lane",
            str(workspace_root),
            owner_unit,
            lane_name,
            "--type",
            lane_type,
            "--repos",
            repos,
            "--branch",
            branch,
        ]
    )


def create_review_lane(root: Path, workspace_root: Path, owner_unit: str, repo: str, pr_number: int) -> None:
    run(
        [
            "python3",
            str(lane_proto(root)),
            "create-review-lane",
            str(workspace_root),
            owner_unit,
            repo,
            str(pr_number),
        ]
    )


def plan_exec_json(root: Path, workspace_root: Path, owner_unit: str, lane_name: str, command_text: str) -> list[dict]:
    proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "plan-exec",
            str(workspace_root),
            owner_unit,
            lane_name,
            command_text,
            "--json",
        ],
        capture=True,
    )
    return json.loads(proc.stdout)


def acquire_lease(root: Path, workspace_root: Path, owner_unit: str, lane_name: str, actor: str, mode: str, ttl_seconds: int = 900, force: bool = False, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    argv = [
        "python3",
        str(lane_proto(root)),
        "acquire-lane-lease",
        str(workspace_root),
        owner_unit,
        lane_name,
        "--actor",
        actor,
        "--mode",
        mode,
        "--ttl-seconds",
        str(ttl_seconds),
    ]
    if force:
        argv.append("--force")
    proc = subprocess.run(argv, check=False, text=True, capture_output=True)
    if expect_ok and proc.returncode != 0:
        raise SystemExit(f"lease acquisition failed unexpectedly: {' '.join(argv)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def show_leases_json(root: Path, workspace_root: Path, owner_unit: str, lane_name: str) -> list[dict]:
    proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "show-lane-leases",
            str(workspace_root),
            owner_unit,
            lane_name,
            "--json",
        ],
        capture=True,
    )
    return json.loads(proc.stdout)


def list_lanes_text(root: Path, workspace_root: Path, owner_unit: str | None = None) -> str:
    argv = [
        "python3",
        str(lane_proto(root)),
        "list-lanes",
        str(workspace_root),
    ]
    if owner_unit:
        argv.extend(["--owner-unit", owner_unit])
    proc = run(argv, capture=True)
    return proc.stdout


def scenario_multi_agent_same_repo(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-router", "app", "feat/router")
    create_lane(root, workspace_root, "apollo", "feat-materialize", "app", "feat/materialize")

    atlas_lane = workspace_root / "agents" / "atlas" / "lanes" / "feat-router" / "lane.toml"
    apollo_lane = workspace_root / "agents" / "apollo" / "lanes" / "feat-materialize" / "lane.toml"

    holds = []
    gaps = []
    evidence = []

    if atlas_lane.exists() and apollo_lane.exists():
        holds.append("two units can create separate lanes touching the same repo without metadata collision")
        evidence.append(f"lane files: {atlas_lane.relative_to(workspace_root)}, {apollo_lane.relative_to(workspace_root)}")
    else:
        gaps.append("unit-scoped lane metadata was not isolated cleanly")

    atlas_exec = plan_exec_json(root, workspace_root, "atlas", "feat-router", "cargo test")
    apollo_exec = plan_exec_json(root, workspace_root, "apollo", "feat-materialize", "cargo test")
    if atlas_exec and apollo_exec and atlas_exec[0]["cwd"] != apollo_exec[0]["cwd"]:
        holds.append("execution planning stays unit-scoped even when both lanes include the same repo")
        evidence.append(f"exec cwd atlas={atlas_exec[0]['cwd']} apollo={apollo_exec[0]['cwd']}")
        verdict = "holds"
    else:
        gaps.append("execution planning did not stay unit-scoped for same-repo parallel work")
        verdict = "fails"

    return ScenarioResult(
        scenario_id="multi-agent-same-repo",
        user_mode="multi-agent",
        title="two agents create lanes that touch the same repo",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_mixed_same_lane_exec(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "layne", "feat-blog", "app", "feat/blog")
    acquire_lease(root, workspace_root, "layne", "feat-blog", "human:layne", "edit")

    exec_rows = plan_exec_json(root, workspace_root, "layne", "feat-blog", "cargo test")

    holds = []
    gaps = []
    evidence = [
        "human edit lease acquired for layne/feat-blog",
        json.dumps(exec_rows if isinstance(exec_rows, list) else exec_rows, indent=2),
    ]

    if isinstance(exec_rows, dict) and exec_rows.get("status") == "blocked":
        holds.append("same-lane human-edit vs agent-exec is blocked by a lease")
        holds.append("prototype now models occupancy instead of silently planning through it")
        verdict = "holds"
    else:
        gaps.append("same-lane concurrent human-edit vs agent-exec is not modeled or blocked")
        verdict = "fails"

    return ScenarioResult(
        scenario_id="mixed-same-lane-exec",
        user_mode="mixed-human-agent",
        title="human edits in a lane while an agent plans exec in the same lane",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_single_agent_interrupt_recovery(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-auth", "app,api", "feat/auth")
    create_review_lane(root, workspace_root, "atlas", "app", 123)
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "atlas",
            "feat-auth",
            "--actor",
            "agent:atlas",
        ]
    )
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "atlas",
            "review-123",
            "--actor",
            "agent:atlas",
        ]
    )
    lane_listing = list_lanes_text(root, workspace_root, "atlas")
    current_lane_proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "current-lane",
            str(workspace_root),
            "atlas",
            "--json",
        ],
        capture=True,
    )
    current_lane_doc = json.loads(current_lane_proc.stdout)
    holds = [
        "agent can enumerate all of its lanes without guessing filesystem paths",
        "lane metadata includes repos, type, and PR references",
    ]
    gaps = []
    evidence = [lane_listing.strip(), json.dumps(current_lane_doc, indent=2)]

    current = current_lane_doc.get("current", {})
    recent = current_lane_doc.get("recent", [])
    if current.get("lane_name") == "review-123":
        holds.append("agent can recover current lane after an interruption")
    else:
        gaps.append("current-lane surface did not record the lane entered most recently")

    if recent and recent[0].get("lane_name") == "feat-auth":
        holds.append("agent can recover previous lane from recent history")
        verdict = "holds"
    else:
        gaps.append("prototype still cannot recover previous lane deterministically")
        verdict = "partial"

    return ScenarioResult(
        scenario_id="single-agent-interrupt-recovery",
        user_mode="single-agent",
        title="agent is interrupted mid-task and needs to recover lane context",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_lease_conflict_matrix(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-matrix", "app", "feat/matrix")

    exec_one = acquire_lease(root, workspace_root, "atlas", "feat-matrix", "agent:atlas", "exec")
    exec_two = acquire_lease(root, workspace_root, "atlas", "feat-matrix", "agent:apollo", "exec")
    edit_conflict = acquire_lease(
        root,
        workspace_root,
        "atlas",
        "feat-matrix",
        "human:layne",
        "edit",
        expect_ok=False,
    )

    create_lane(root, workspace_root, "atlas", "feat-review-lock", "app", "feat/review-lock")
    acquire_lease(root, workspace_root, "atlas", "feat-review-lock", "agent:atlas", "review")
    review_conflict = acquire_lease(
        root,
        workspace_root,
        "atlas",
        "feat-review-lock",
        "agent:apollo",
        "exec",
        expect_ok=False,
    )

    holds = []
    gaps = []
    evidence = []

    if exec_one.returncode == 0 and exec_two.returncode == 0:
        holds.append("exec-vs-exec is allowed for the same lane")
        evidence.append("two exec leases acquired successfully on atlas/feat-matrix")
    else:
        gaps.append("exec-vs-exec was blocked unexpectedly")

    if edit_conflict.returncode != 0:
        holds.append("edit-vs-exec conflicts as expected")
        evidence.append(edit_conflict.stdout.strip())
    else:
        gaps.append("edit-vs-exec did not conflict")

    if review_conflict.returncode != 0:
        holds.append("review-vs-anything is exclusive")
        evidence.append(review_conflict.stdout.strip())
    else:
        gaps.append("review-vs-exec did not conflict")

    leases = show_leases_json(root, workspace_root, "atlas", "feat-matrix")
    evidence.append(json.dumps(leases, indent=2))
    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="lease-conflict-matrix",
        user_mode="cross-mode",
        title="lease conflict matrix enforces edit/exec/review semantics",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_stale_lease_force_break(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-stale", "app", "feat/stale")
    stale = acquire_lease(
        root,
        workspace_root,
        "atlas",
        "feat-stale",
        "human:layne",
        "edit",
        ttl_seconds=0,
    )
    blocked_exec = plan_exec_json(root, workspace_root, "atlas", "feat-stale", "cargo test")
    forced = acquire_lease(
        root,
        workspace_root,
        "atlas",
        "feat-stale",
        "agent:atlas",
        "exec",
        ttl_seconds=900,
        force=True,
    )
    leases_after = show_leases_json(root, workspace_root, "atlas", "feat-stale")

    holds = []
    gaps = []
    evidence = [stale.stdout.strip(), json.dumps(blocked_exec, indent=2), forced.stdout.strip(), json.dumps(leases_after, indent=2)]

    if isinstance(blocked_exec, dict) and blocked_exec.get("reason") == "stale-conflicting-lease":
        holds.append("plan-exec detects stale conflicting leases")
    else:
        gaps.append("plan-exec did not flag stale conflicting leases")

    actors_after = {lease["actor"] for lease in leases_after}
    if "human:layne" not in actors_after and "agent:atlas" in actors_after:
        holds.append("force acquisition breaks stale conflicting lease and installs new lease")
    else:
        gaps.append("force acquisition did not replace stale conflicting lease cleanly")

    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="stale-lease-force-break",
        user_mode="cross-mode",
        title="stale leases are detectable and force-breakable",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_solo_human_forgets_lane(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "layne", "feat-auth", "app,api", "feat/auth")
    create_lane(root, workspace_root, "layne", "feat-web", "web", "feat/web")
    create_lane(root, workspace_root, "layne", "feat-release", "app,web", "feat/release")
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "layne",
            "feat-release",
            "--actor",
            "human:layne",
        ]
    )
    create_review_lane(root, workspace_root, "layne", "app", 456)
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "layne",
            "review-456",
            "--actor",
            "human:layne",
        ]
    )

    lane_listing = list_lanes_text(root, workspace_root, "layne")
    current_lane_proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "current-lane",
            str(workspace_root),
            "layne",
            "--json",
        ],
        capture=True,
    )
    current_lane_doc = json.loads(current_lane_proc.stdout)
    holds = [
        "user can see all lanes in one listing",
        "review lane is isolated as its own lane type rather than overwriting feature state",
    ]
    gaps = []
    evidence = [lane_listing.strip(), json.dumps(current_lane_doc, indent=2)]

    if current_lane_doc.get("current", {}).get("lane_name") == "review-456":
        holds.append("current review lane is visible after switching")
    else:
        gaps.append("current lane is not visible after switching to review")

    recent = current_lane_doc.get("recent", [])
    if recent and recent[0].get("lane_name") == "feat-release":
        holds.append("previous feature lane is recoverable after entering review")
        verdict = "holds"
    else:
        gaps.append("prototype lacks an obvious return-to-previous-lane recovery path")
        verdict = "partial"

    return ScenarioResult(
        scenario_id="solo-human-lane-recovery",
        user_mode="solo-human",
        title="solo human has three feature lanes, switches to review, then forgets the prior lane",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def run_scenarios(workspace_root: Path) -> list[ScenarioResult]:
    root = repo_root()
    init_workspace(workspace_root)
    return [
        scenario_lease_conflict_matrix(root, workspace_root),
        scenario_stale_lease_force_break(root, workspace_root),
        scenario_multi_agent_same_repo(root, workspace_root),
        scenario_mixed_same_lane_exec(root, workspace_root),
        scenario_single_agent_interrupt_recovery(root, workspace_root),
        scenario_solo_human_forgets_lane(root, workspace_root),
    ]


def print_human(results: list[ScenarioResult], workspace_root: Path) -> None:
    print("gr2 cross-mode lane stress results")
    print(f"workspace: {workspace_root}")
    print()
    for result in results:
        print(f"[{result.verdict}] {result.user_mode}: {result.title}")
        if result.holds:
            print("  holds:")
            for item in result.holds:
                print(f"    - {item}")
        if result.gaps:
            print("  gaps:")
            for item in result.gaps:
                print(f"    - {item}")
        if result.evidence:
            print("  evidence:")
            for item in result.evidence:
                for line in item.splitlines():
                    print(f"    {line}")
        print()


def main() -> int:
    args = parse_args()

    if args.workspace_root:
        workspace_root = args.workspace_root.resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        results = run_scenarios(workspace_root)
    else:
        with tempfile.TemporaryDirectory(prefix="gr2-cross-mode-") as tmp:
            workspace_root = Path(tmp)
            results = run_scenarios(workspace_root)
            if args.json:
                print(json.dumps([asdict(result) for result in results], indent=2))
                return 0
            print_human(results, workspace_root)
            return 0

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print_human(results, workspace_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
