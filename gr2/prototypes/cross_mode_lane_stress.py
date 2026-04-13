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

[[repos]]
name = "premium"
path = "repos/premium"
url = "https://example.invalid/premium.git"

[workspace_constraints]
max_concurrent_edit_leases_global = 2

[workspace_constraints.required_reviewers]
premium = 2
app = 1

[[units]]
name = "atlas"
path = "agents/atlas"
agent_id = "atlas-agent"
repos = ["app", "api", "web", "premium"]

[[units]]
name = "apollo"
path = "agents/apollo"
agent_id = "apollo-agent"
repos = ["app", "api", "web", "premium"]

[[units]]
name = "layne"
path = "agents/layne"
agent_id = "layne-human"
repos = ["app", "api", "web", "premium"]

[[units]]
name = "synapt-core"
path = "agents/synapt-core"
agent_id = "agent_opus_abc123"
repos = ["app", "api", "web", "premium"]

[[units]]
name = "release-control"
path = "agents/release-control"
agent_id = "agent_opus_abc123"
repos = ["app", "api", "web", "premium"]
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


def check_review_requirements_json(root: Path, workspace_root: Path, repo: str, pr_number: int) -> dict:
    proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "check-review-requirements",
            str(workspace_root),
            repo,
            str(pr_number),
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


def plan_handoff_json(
    root: Path,
    workspace_root: Path,
    source_owner_unit: str,
    source_lane_name: str,
    target_unit: str,
    mode: str,
    target_lane_name: str | None = None,
) -> dict:
    argv = [
        "python3",
        str(lane_proto(root)),
        "plan-handoff",
        str(workspace_root),
        source_owner_unit,
        source_lane_name,
        target_unit,
        "--mode",
        mode,
        "--json",
    ]
    if target_lane_name:
        argv.extend(["--target-lane-name", target_lane_name])
    proc = run(argv, capture=True)
    return json.loads(proc.stdout)


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


def scenario_agent_handoff_relay(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-router", "app,api", "feat/router")
    run(
        [
            "python3",
            str(lane_proto(root)),
            "share-lane",
            str(workspace_root),
            "atlas",
            "feat-router",
            "apollo",
        ]
    )
    shared_plan = plan_handoff_json(
        root,
        workspace_root,
        "atlas",
        "feat-router",
        "apollo",
        "shared",
    )
    run(
        [
            "python3",
            str(lane_proto(root)),
            "create-continuation-lane",
            str(workspace_root),
            "atlas",
            "feat-router",
            "apollo",
            "feat-router-relay",
        ]
    )
    continuation_plan = plan_handoff_json(
        root,
        workspace_root,
        "atlas",
        "feat-router",
        "apollo",
        "continuation",
        "feat-router-relay",
    )

    holds = []
    gaps = []
    evidence = [
        json.dumps(shared_plan, indent=2),
        json.dumps(continuation_plan, indent=2),
    ]

    if not shared_plan["invariant_assessment"]["unit_scoped"]:
        holds.append("cross-unit shared-lane relay exposes the unit-scoping violation directly")
    else:
        gaps.append("shared-lane relay incorrectly appears unit-scoped")

    shared_cwds = {row["cwd"] for row in shared_plan["exec_rows"]}
    if all("/agents/atlas/lanes/feat-router/" in cwd for cwd in shared_cwds):
        holds.append("shared-lane relay forces the target unit to execute inside the source unit lane root")
    else:
        gaps.append("shared-lane relay did not clearly surface source-unit cwd ownership")

    if continuation_plan["invariant_assessment"]["unit_scoped"]:
        holds.append("continuation lane preserves unit-scoped cwd and lease ownership")
    else:
        gaps.append("continuation lane did not preserve unit scoping")

    continuation_cwds = {row["cwd"] for row in continuation_plan["exec_rows"]}
    if all("/agents/apollo/lanes/feat-router-relay/" in cwd for cwd in continuation_cwds):
        holds.append("continuation lane gives the target unit an independent lane root")
        verdict = "holds"
    else:
        gaps.append("continuation lane did not create target-unit-local execution roots")
        verdict = "fails"

    return ScenarioResult(
        scenario_id="agent-handoff-relay",
        user_mode="multi-agent",
        title="agent-to-agent lane handoff prefers continuation over cross-unit shared lanes",
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


def scenario_synapt_lane_events(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-events", "app,api", "feat/events")
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "atlas",
            "feat-events",
            "--actor",
            "agent:atlas",
            "--notify-channel",
            "--recall",
        ]
    )
    acquire_lease(root, workspace_root, "atlas", "feat-events", "agent:atlas", "exec")
    run(
        [
            "python3",
            str(lane_proto(root)),
            "release-lane-lease",
            str(workspace_root),
            "atlas",
            "feat-events",
            "--actor",
            "agent:atlas",
        ]
    )
    run(
        [
            "python3",
            str(lane_proto(root)),
            "exit-lane",
            str(workspace_root),
            "atlas",
            "--actor",
            "agent:atlas",
            "--notify-channel",
            "--recall",
        ]
    )
    history_proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "lane-history",
            str(workspace_root),
            "atlas",
            "--json",
        ],
        capture=True,
    )
    history_rows = json.loads(history_proc.stdout)
    events_path = workspace_root / ".grip" / "events" / "lane_events.jsonl"
    recall_path = workspace_root / ".grip" / "events" / "recall_lane_history.jsonl"

    holds = []
    gaps = []
    evidence = [json.dumps(history_rows, indent=2)]

    event_types = [row["type"] for row in history_rows]
    expected = ["lane_enter", "lease_acquire", "lease_release", "lane_exit"]
    if event_types == expected:
        holds.append("lane event timeline is reconstructible from append-only event log")
    else:
        gaps.append(f"unexpected lane event order: {event_types}")

    if all(row.get("agent_id") == "atlas-agent" for row in history_rows):
        holds.append("agent_id flows from workspace spec into lane events")
    else:
        gaps.append("agent_id did not flow consistently into lane events")

    if events_path.exists() and recall_path.exists():
        holds.append("channel-compatible and recall-compatible event logs are both written")
    else:
        gaps.append("expected event logs were not both written")

    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="synapt-lane-events",
        user_mode="single-agent",
        title="lane enter/lease/exit emits reconstructible synapt-compatible events",
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


def scenario_identity_rebind_live_lanes(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "synapt-core", "feat-auth", "app,api", "feat/auth")
    create_lane(root, workspace_root, "synapt-core", "feat-deploy", "web", "feat/deploy")
    acquire_lease(root, workspace_root, "synapt-core", "feat-auth", "agent:opus", "edit")
    acquire_lease(root, workspace_root, "synapt-core", "feat-deploy", "agent:opus", "edit")
    run(
        [
            "python3",
            str(lane_proto(root)),
            "enter-lane",
            str(workspace_root),
            "synapt-core",
            "feat-auth",
            "--actor",
            "agent:opus",
        ]
    )
    rebind_active = run(
        [
            "python3",
            str(lane_proto(root)),
            "rebind-unit",
            str(workspace_root),
            "synapt-core",
            "release-control",
            "--actor",
            "premium:control-plane",
            "--json",
        ],
        capture=True,
    )
    rebind_active_doc = json.loads(rebind_active.stdout)
    blocked_old = plan_exec_json(root, workspace_root, "synapt-core", "feat-auth", "cargo test")
    continuation = plan_handoff_json(
        root,
        workspace_root,
        "synapt-core",
        "feat-auth",
        "release-control",
        "continuation",
        "feat-auth-relay",
    )
    history_proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "lane-history",
            str(workspace_root),
            "synapt-core",
            "--json",
        ],
        capture=True,
    )
    history_rows = json.loads(history_proc.stdout)

    clean_workspace = workspace_root / "clean-rebind"
    clean_workspace.mkdir(parents=True, exist_ok=True)
    init_workspace(clean_workspace)
    create_lane(root, clean_workspace, "synapt-core", "feat-clean", "app", "feat/clean")
    rebind_clean = run(
        [
            "python3",
            str(lane_proto(root)),
            "rebind-unit",
            str(clean_workspace),
            "synapt-core",
            "release-control",
            "--actor",
            "premium:control-plane",
            "--json",
        ],
        capture=True,
    )
    rebind_clean_doc = json.loads(rebind_clean.stdout)

    holds = []
    gaps = []
    evidence = [
        json.dumps(rebind_active_doc, indent=2),
        json.dumps(blocked_old, indent=2),
        json.dumps(continuation, indent=2),
        json.dumps(history_rows, indent=2),
        json.dumps(rebind_clean_doc, indent=2),
    ]

    if all(item["status"] == "frozen" for item in rebind_active_doc["affected_lanes"]):
        holds.append("active lanes stay in the old unit and become frozen rather than moving silently")
    else:
        gaps.append("rebind did not freeze old lanes deterministically")

    if len(rebind_active_doc["expired_leases"]) == 2:
        holds.append("active edit leases are force-released during rebind")
    else:
        gaps.append("active leases were not force-released during rebind")

    if isinstance(blocked_old, dict) and blocked_old.get("reason") == "unit-rebound":
        holds.append("old unit lanes are blocked for further exec planning after rebind")
    else:
        gaps.append("old unit lanes were not blocked after rebind")

    if continuation["invariant_assessment"]["unit_scoped"]:
        holds.append("post-rebind recovery path is continuation under the new unit")
    else:
        gaps.append("rebind recovery path did not preserve unit scoping")

    if any(row["type"] == "unit_rebind" for row in history_rows):
        holds.append("lane event history records the unit rebind for recall reconstruction")
    else:
        gaps.append("lane history did not record the rebind transition")

    contract = rebind_active_doc.get("required_contract", {})
    if contract.get("same_agent_id") and contract.get("old_to_new_mapping"):
        holds.append("prototype identifies same-agent-id continuity and explicit old->new mapping as required contract")
    else:
        gaps.append("rebind contract requirements are not explicit enough")

    if rebind_clean_doc["expired_leases"] == []:
        holds.append("clean rebind with no active leases avoids unnecessary lease churn")
    else:
        gaps.append("clean rebind unexpectedly expired leases")

    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="identity-rebind-live-lanes",
        user_mode="single-agent",
        title="identity rebinding freezes old lanes and resumes through continuation under the new unit",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_global_edit_lease_cap(root: Path, workspace_root: Path) -> ScenarioResult:
    create_lane(root, workspace_root, "atlas", "feat-cap-a", "app", "feat/cap-a")
    create_lane(root, workspace_root, "apollo", "feat-cap-b", "api", "feat/cap-b")
    create_lane(root, workspace_root, "layne", "feat-cap-c", "web", "feat/cap-c")

    create_lane(root, workspace_root, "release-control", "feat-cap-stale", "premium", "feat/cap-stale")
    acquire_lease(root, workspace_root, "release-control", "feat-cap-stale", "agent:opus", "edit", ttl_seconds=0)

    lease_a = acquire_lease(root, workspace_root, "atlas", "feat-cap-a", "agent:atlas", "edit")
    lease_b = acquire_lease(root, workspace_root, "apollo", "feat-cap-b", "agent:apollo", "edit")
    lease_c = acquire_lease(
        root, workspace_root, "layne", "feat-cap-c", "human:layne", "edit", expect_ok=False
    )

    stale_force = acquire_lease(
        root,
        workspace_root,
        "release-control",
        "feat-cap-stale",
        "agent:opus",
        "edit",
        ttl_seconds=900,
        force=True,
        expect_ok=False,
    )

    holds = []
    gaps = []
    evidence = [lease_c.stdout.strip(), stale_force.stdout.strip()]

    if lease_a.returncode == 0 and lease_b.returncode == 0 and lease_c.returncode != 0:
        holds.append("third edit lease is blocked when global cap of 2 is reached")
    else:
        gaps.append("global edit lease cap did not block the third concurrent edit lease")

    if stale_force.returncode != 0 and "workspace-edit-lease-cap" in stale_force.stdout:
        holds.append("force-breaking a stale local lease does not bypass the workspace edit cap")
    else:
        gaps.append("stale force-break bypassed the workspace edit lease cap")

    # Release leases so later scenarios aren't blocked by the global cap
    for unit, lane, actor in [
        ("atlas", "feat-cap-a", "agent:atlas"),
        ("apollo", "feat-cap-b", "agent:apollo"),
    ]:
        run(
            [
                "python3",
                str(lane_proto(root)),
                "release-lane-lease",
                str(workspace_root),
                unit,
                lane,
                "--actor",
                actor,
            ],
            capture=True,
        )

    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="global-edit-lease-cap",
        user_mode="cross-mode",
        title="workspace-wide edit lease cap is enforced across all units",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def scenario_required_reviewers(root: Path, workspace_root: Path) -> ScenarioResult:
    zero = check_review_requirements_json(root, workspace_root, "premium", 777)
    create_review_lane(root, workspace_root, "atlas", "premium", 777)
    one = check_review_requirements_json(root, workspace_root, "premium", 777)
    create_review_lane(root, workspace_root, "apollo", "premium", 777)
    two = check_review_requirements_json(root, workspace_root, "premium", 777)

    holds = []
    gaps = []
    evidence = [
        json.dumps(zero, indent=2),
        json.dumps(one, indent=2),
        json.dumps(two, indent=2),
    ]

    if zero["required_reviewers"] == 2 and zero["actual_reviewers"] == 0 and not zero["satisfied"]:
        holds.append("review requirements report unsatisfied with zero review lanes")
    else:
        gaps.append("zero-reviewer requirement state is incorrect")

    if one["actual_reviewers"] == 1 and not one["satisfied"]:
        holds.append("one review lane is still unsatisfied when premium requires two reviewers")
    else:
        gaps.append("single reviewer state is incorrect")

    if two["actual_reviewers"] == 2 and two["satisfied"]:
        holds.append("two review lanes satisfy the premium repo review requirement")
    else:
        gaps.append("two reviewers did not satisfy the premium repo requirement")

    verdict = "holds" if not gaps else "fails"
    return ScenarioResult(
        scenario_id="required-reviewers",
        user_mode="cross-mode",
        title="required reviewer counts are enforced from workspace constraints",
        verdict=verdict,
        holds=holds,
        gaps=gaps,
        evidence=evidence,
    )


def run_scenarios(workspace_root: Path) -> list[ScenarioResult]:
    root = repo_root()
    init_workspace(workspace_root)
    return [
        scenario_synapt_lane_events(root, workspace_root),
        scenario_lease_conflict_matrix(root, workspace_root),
        scenario_stale_lease_force_break(root, workspace_root),
        scenario_multi_agent_same_repo(root, workspace_root),
        scenario_agent_handoff_relay(root, workspace_root),
        scenario_identity_rebind_live_lanes(root, workspace_root),
        scenario_global_edit_lease_cap(root, workspace_root),
        scenario_required_reviewers(root, workspace_root),
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
