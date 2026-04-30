#!/usr/bin/env python3
"""Prototype premium org/policy -> WorkspaceSpec compilation seam.

Premium owns:
- org config
- roles and entitlements
- reviewer requirements
- global policy limits

gr2 consumes only the compiled workspace-scoped result.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prototype premium org/policy -> WorkspaceSpec compilation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo")
    demo.add_argument("--json", action="store_true")

    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument(
        "--scenario",
        choices=["baseline", "repo-update", "downgrade"],
        default="baseline",
    )
    compile_cmd.add_argument("--json", action="store_true")

    return parser.parse_args()


def premium_org_state() -> dict[str, Any]:
    return {
        "team_id": "team_synapt_core",
        "workspace_id": "ws_synapt_core",
        "workspace_name": "synapt-core",
        "repos": ["grip", "premium", "recall", "config", "tests"],
        "policy": {
            "max_concurrent_edit_leases_global": 2,
            "lane_naming_convention": "<kind>-<scope>",
            "required_reviewers": {
                "premium": 2,
                "grip": 1,
                "recall": 1,
            },
        },
        "roles": {
            "builder": {
                "repo_access": ["grip", "premium", "recall", "config", "tests"],
                "lane_limit": 3,
                "allowed_lane_kinds": ["feature", "review", "scratch"],
            },
            "qa": {
                "repo_access": ["tests", "grip", "recall"],
                "lane_limit": 2,
                "allowed_lane_kinds": ["review", "scratch"],
            },
            "design": {
                "repo_access": ["grip", "premium", "config"],
                "lane_limit": 3,
                "allowed_lane_kinds": ["feature", "review", "scratch"],
            },
        },
        "agents": [
            {
                "handle": "opus",
                "persistent_id": "agent_opus_abc123",
                "owner_unit": "release-control",
                "role": "builder",
                "entitlements": ["premium", "channels", "recall", "multi_lane"],
            },
            {
                "handle": "sentinel",
                "persistent_id": "agent_sentinel_def456",
                "owner_unit": "qa-sentinel",
                "role": "qa",
                "entitlements": ["premium", "channels", "recall"],
            },
            {
                "handle": "atlas",
                "persistent_id": "agent_atlas_ghi789",
                "owner_unit": "design-research",
                "role": "design",
                "entitlements": ["premium", "channels", "recall", "multi_lane"],
            },
        ],
    }


def repo_update_state() -> dict[str, Any]:
    state = deepcopy(premium_org_state())
    state["repos"].append("mission-control")
    state["roles"]["builder"]["repo_access"].append("mission-control")
    state["roles"]["design"]["repo_access"].append("mission-control")
    state["policy"]["required_reviewers"]["mission-control"] = 2
    return state


def downgrade_state() -> dict[str, Any]:
    state = deepcopy(premium_org_state())
    for agent in state["agents"]:
        if agent["handle"] == "sentinel":
            agent["entitlements"] = []
    return state


def compile_agent_unit(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    role_doc = state["roles"][agent["role"]]
    entitlements = set(agent.get("entitlements", []))
    premium_enabled = "premium" in entitlements

    if premium_enabled:
        repo_access = role_doc["repo_access"]
        lane_limit = role_doc["lane_limit"]
        allowed_lane_kinds = role_doc["allowed_lane_kinds"]
        channels_enabled = "channels" in entitlements
        recall_enabled = "recall" in entitlements
    else:
        repo_access = ["grip"]
        lane_limit = 1
        allowed_lane_kinds = ["feature"]
        channels_enabled = False
        recall_enabled = False

    return {
        "name": agent["owner_unit"],
        "path": f"agents/{agent['owner_unit']}",
        "agent_id": agent["persistent_id"],
        "repos": repo_access,
        "constraints": {
            "lane_limit": lane_limit,
            "allowed_lane_kinds": allowed_lane_kinds,
            "channels_enabled": channels_enabled,
            "recall_enabled": recall_enabled,
        },
    }


def compile_workspace_spec(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_name": state["workspace_name"],
        "workspace_id": state["workspace_id"],
        "repos": [{"name": repo, "path": f"repos/{repo}"} for repo in state["repos"]],
        "units": [compile_agent_unit(agent, state) for agent in state["agents"]],
        "workspace_constraints": {
            "max_concurrent_edit_leases_global": state["policy"]["max_concurrent_edit_leases_global"],
            "lane_naming_convention": state["policy"]["lane_naming_convention"],
            "required_reviewers": state["policy"]["required_reviewers"],
        },
    }


def premium_view(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_id": state["team_id"],
        "workspace_id": state["workspace_id"],
        "agents": [
            {
                "handle": agent["handle"],
                "persistent_id": agent["persistent_id"],
                "role": agent["role"],
                "entitlements": agent["entitlements"],
                "owner_unit": agent["owner_unit"],
            }
            for agent in state["agents"]
        ],
        "policy": state["policy"],
        "notes": [
            "premium owns role evaluation and entitlement interpretation",
            "premium decides how org policy degrades when entitlements are removed",
            "gr2 should not infer role semantics from raw org config",
        ],
    }


def scenario_bundle() -> dict[str, Any]:
    baseline = premium_org_state()
    repo_update = repo_update_state()
    downgrade = downgrade_state()
    return {
        "baseline": {
            "premium_knows": premium_view(baseline),
            "gr2_sees": compile_workspace_spec(baseline),
            "summary": "org with 3 agents, 5 repos, max 2 concurrent edit leases globally",
        },
        "role_access": {
            "builder_repos": compile_agent_unit(baseline["agents"][0], baseline)["repos"],
            "qa_repos": compile_agent_unit(baseline["agents"][1], baseline)["repos"],
            "summary": "builders get all repos, QA gets test-focused access only",
        },
        "repo_update": {
            "before": compile_workspace_spec(baseline),
            "after": compile_workspace_spec(repo_update),
            "summary": "admin adds mission-control mid-sprint; affected units get updated access on recompilation",
        },
        "downgrade": {
            "before": compile_workspace_spec(baseline),
            "after": compile_workspace_spec(downgrade),
            "summary": "loss of premium degrades one unit to OSS defaults without injecting org logic into gr2",
        },
    }


def print_human(payload: dict[str, Any]) -> None:
    baseline = payload["baseline"]
    print("gr2 org/policy compiler prototype")
    print()
    print(f"workspace: {baseline['gr2_sees']['workspace_name']}")
    print(
        f"global edit lease cap: {baseline['gr2_sees']['workspace_constraints']['max_concurrent_edit_leases_global']}"
    )
    print("units gr2 sees:")
    for unit in baseline["gr2_sees"]["units"]:
        constraints = unit["constraints"]
        print(
            f"- {unit['name']} repos={','.join(unit['repos'])} lane_limit={constraints['lane_limit']} "
            f"channels={constraints['channels_enabled']} recall={constraints['recall_enabled']}"
        )
    print()
    print("role-based access")
    print(f"- builder repos: {','.join(payload['role_access']['builder_repos'])}")
    print(f"- qa repos: {','.join(payload['role_access']['qa_repos'])}")
    print()
    print("repo update")
    before_repos = payload["repo_update"]["before"]["repos"]
    after_repos = payload["repo_update"]["after"]["repos"]
    print(f"- before repos: {','.join(item['name'] for item in before_repos)}")
    print(f"- after repos:  {','.join(item['name'] for item in after_repos)}")
    print()
    print("downgrade")
    before_units = {
        unit["name"]: unit for unit in payload["downgrade"]["before"]["units"]
    }
    after_units = {
        unit["name"]: unit for unit in payload["downgrade"]["after"]["units"]
    }
    target = "qa-sentinel"
    print(
        f"- {target} before: repos={','.join(before_units[target]['repos'])} lane_limit={before_units[target]['constraints']['lane_limit']}"
    )
    print(
        f"- {target} after:  repos={','.join(after_units[target]['repos'])} lane_limit={after_units[target]['constraints']['lane_limit']}"
    )


def main() -> int:
    args = parse_args()
    if args.command == "demo":
        payload = scenario_bundle()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_human(payload)
        return 0
    if args.command == "compile":
        if args.scenario == "baseline":
            state = premium_org_state()
        elif args.scenario == "repo-update":
            state = repo_update_state()
        else:
            state = downgrade_state()
        payload = {
            "premium_knows": premium_view(state),
            "gr2_sees": compile_workspace_spec(state),
        }
        print(json.dumps(payload, indent=2))
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
