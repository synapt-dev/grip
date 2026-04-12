#!/usr/bin/env python3
"""Prototype premium-owned identity -> gr2 unit binding.

This models the integration seam where Premium owns durable agent identity and
workspace assignment, while gr2 consumes only the compiled workspace-scoped
unit view.

Key rule:
- Premium resolves persistent identity and org membership.
- gr2 only materializes units and lane policy from the compiled spec.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Any


@dataclasses.dataclass
class AgentIdentity:
    handle: str
    persistent_id: str
    kind: str


@dataclasses.dataclass
class WorkspaceAssignment:
    workspace_id: str
    workspace_name: str
    owner_unit: str
    unit_path: str
    repo_access: list[str]
    lane_limit: int
    role: str
    active: bool = True


@dataclasses.dataclass
class PremiumAgentRecord:
    identity: AgentIdentity
    assignments: list[WorkspaceAssignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prototype premium identity -> gr2 unit binding"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo")
    demo.add_argument("--json", action="store_true")

    resolve_cmd = sub.add_parser("resolve-binding")
    resolve_cmd.add_argument("workspace_id")
    resolve_cmd.add_argument("handle")
    resolve_cmd.add_argument("--json", action="store_true")

    compile_cmd = sub.add_parser("compile-workspace")
    compile_cmd.add_argument("workspace_id")
    compile_cmd.add_argument(
        "--scenario",
        choices=["baseline", "reassigned"],
        default="baseline",
    )
    compile_cmd.add_argument("--json", action="store_true")

    return parser.parse_args()


def sample_org_state() -> dict[str, PremiumAgentRecord]:
    return {
        "opus": PremiumAgentRecord(
            identity=AgentIdentity(
                handle="opus",
                persistent_id="agent_opus_abc123",
                kind="agent",
            ),
            assignments=[
                WorkspaceAssignment(
                    workspace_id="ws_synapt_core",
                    workspace_name="synapt-core",
                    owner_unit="synapt-core",
                    unit_path="agents/synapt-core",
                    repo_access=["grip", "premium", "recall"],
                    lane_limit=2,
                    role="core-agent",
                ),
                WorkspaceAssignment(
                    workspace_id="ws_blog",
                    workspace_name="blog-studio",
                    owner_unit="editorial-opus",
                    unit_path="agents/editorial-opus",
                    repo_access=["blog", "marketing-site"],
                    lane_limit=1,
                    role="editorial-agent",
                ),
            ],
        ),
        "apollo": PremiumAgentRecord(
            identity=AgentIdentity(
                handle="apollo",
                persistent_id="agent_apollo_def456",
                kind="agent",
            ),
            assignments=[
                WorkspaceAssignment(
                    workspace_id="ws_synapt_core",
                    workspace_name="synapt-core",
                    owner_unit="materialization",
                    unit_path="agents/materialization",
                    repo_access=["grip", "premium"],
                    lane_limit=2,
                    role="build-agent",
                )
            ],
        ),
        "atlas": PremiumAgentRecord(
            identity=AgentIdentity(
                handle="atlas",
                persistent_id="agent_atlas_ghi789",
                kind="agent",
            ),
            assignments=[
                WorkspaceAssignment(
                    workspace_id="ws_synapt_core",
                    workspace_name="synapt-core",
                    owner_unit="design-research",
                    unit_path="agents/design-research",
                    repo_access=["grip", "premium", "recall", "config"],
                    lane_limit=3,
                    role="design-agent",
                )
            ],
        ),
    }


def reassigned_org_state() -> dict[str, PremiumAgentRecord]:
    state = sample_org_state()
    opus = state["opus"]
    retained = [
        assignment
        for assignment in opus.assignments
        if assignment.workspace_id != "ws_synapt_core"
    ]
    opus.assignments = [
        WorkspaceAssignment(
            workspace_id="ws_synapt_core",
            workspace_name="synapt-core",
            owner_unit="release-control",
            unit_path="agents/release-control",
            repo_access=["grip", "premium", "recall"],
            lane_limit=2,
            role="release-agent",
        ),
        *retained,
    ]
    return state


def resolve_binding(state: dict[str, PremiumAgentRecord], workspace_id: str, handle: str) -> dict[str, Any]:
    record = state.get(handle)
    if not record:
        raise SystemExit(f"unknown agent handle: {handle}")
    for assignment in record.assignments:
        if assignment.workspace_id == workspace_id and assignment.active:
            return {
                "premium_identity": {
                    "handle": record.identity.handle,
                    "persistent_id": record.identity.persistent_id,
                    "kind": record.identity.kind,
                },
                "workspace_binding": dataclasses.asdict(assignment),
                "gr2_view": {
                    "owner_unit": assignment.owner_unit,
                    "unit_path": assignment.unit_path,
                    "repo_access": assignment.repo_access,
                    "lane_limit": assignment.lane_limit,
                    "agent_id": record.identity.persistent_id,
                },
            }
    raise SystemExit(f"no active assignment for {handle} in workspace {workspace_id}")


def compile_workspace_spec_view(state: dict[str, PremiumAgentRecord], workspace_id: str) -> dict[str, Any]:
    units: list[dict[str, Any]] = []
    premium_bindings: list[dict[str, Any]] = []
    for record in state.values():
        for assignment in record.assignments:
            if assignment.workspace_id != workspace_id or not assignment.active:
                continue
            premium_bindings.append(
                {
                    "handle": record.identity.handle,
                    "persistent_id": record.identity.persistent_id,
                    "workspace_id": assignment.workspace_id,
                    "owner_unit": assignment.owner_unit,
                    "role": assignment.role,
                }
            )
            units.append(
                {
                    "name": assignment.owner_unit,
                    "path": assignment.unit_path,
                    "agent_id": record.identity.persistent_id,
                    "repos": assignment.repo_access,
                    "policy": {
                        "lane_limit": assignment.lane_limit,
                        "role": assignment.role,
                    },
                }
            )
    return {
        "premium_knows": {
            "workspace_id": workspace_id,
            "bindings": premium_bindings,
            "notes": [
                "persistent identity is resolved in premium",
                "premium owns reassignment and org membership",
                "premium can compile different owner_unit names per workspace for the same agent",
            ],
        },
        "gr2_sees": {
            "workspace_spec_fragment": {
                "workspace_id": workspace_id,
                "units": units,
            },
            "notes": [
                "gr2 consumes workspace-scoped units only",
                "agent_id is an opaque identifier for attribution, not org resolution logic",
                "gr2 does not decide who an agent is or where else it is assigned",
            ],
        },
    }


def demo_payload() -> dict[str, Any]:
    baseline = sample_org_state()
    reassigned = reassigned_org_state()
    return {
        "design_rule": {
            "premium_owns": [
                "persistent agent identity",
                "org membership",
                "workspace assignment",
                "reassignment history",
            ],
            "gr2_owns": [
                "workspace-scoped unit directories",
                "lane and lease enforcement",
                "local execution surfaces",
            ],
        },
        "same_agent_two_workspaces": {
            "synapt_core": resolve_binding(baseline, "ws_synapt_core", "opus"),
            "blog_workspace": resolve_binding(baseline, "ws_blog", "opus"),
            "explanation": "one persistent agent can bind to different owner_unit names in different workspaces",
        },
        "org_reassignment": {
            "before": resolve_binding(baseline, "ws_synapt_core", "opus"),
            "after": resolve_binding(reassigned, "ws_synapt_core", "opus"),
            "explanation": "premium changes the binding and recompiles the workspace view; gr2 does not infer reassignment itself",
        },
        "compiled_workspace_view": compile_workspace_spec_view(baseline, "ws_synapt_core"),
    }


def print_human(payload: dict[str, Any]) -> None:
    print("gr2 identity -> unit binding prototype")
    print()
    print("Premium owns:")
    for item in payload["design_rule"]["premium_owns"]:
        print(f"- {item}")
    print("gr2 owns:")
    for item in payload["design_rule"]["gr2_owns"]:
        print(f"- {item}")
    print()
    same = payload["same_agent_two_workspaces"]
    print("Same agent across two workspaces")
    print(
        f"- opus in synapt-core -> {same['synapt_core']['workspace_binding']['owner_unit']}"
    )
    print(
        f"- opus in blog-studio -> {same['blog_workspace']['workspace_binding']['owner_unit']}"
    )
    print(f"- {same['explanation']}")
    print()
    reassignment = payload["org_reassignment"]
    print("Org reassignment")
    print(
        f"- before: {reassignment['before']['workspace_binding']['owner_unit']}"
    )
    print(
        f"- after:  {reassignment['after']['workspace_binding']['owner_unit']}"
    )
    print(f"- {reassignment['explanation']}")
    print()
    compiled = payload["compiled_workspace_view"]
    print("Compiled workspace fragment gr2 sees")
    for unit in compiled["gr2_sees"]["workspace_spec_fragment"]["units"]:
        print(
            f"- unit={unit['name']} agent_id={unit['agent_id']} repos={','.join(unit['repos'])} lane_limit={unit['policy']['lane_limit']}"
        )


def main() -> int:
    args = parse_args()
    if args.command == "demo":
        payload = demo_payload()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_human(payload)
        return 0
    if args.command == "resolve-binding":
        payload = resolve_binding(sample_org_state(), args.workspace_id, args.handle)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(payload, indent=2))
        return 0
    if args.command == "compile-workspace":
        state = sample_org_state() if args.scenario == "baseline" else reassigned_org_state()
        payload = compile_workspace_spec_view(state, args.workspace_id)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(payload, indent=2))
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
