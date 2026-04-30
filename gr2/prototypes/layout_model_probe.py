#!/usr/bin/env python3
"""Assess which workspace layout model best matches the observed gr2 behavior.

This is a UX prototype. It does not prescribe the final architecture on its own.
It makes the current mismatch explicit so we can iterate with evidence.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe observed gr2 layout vs candidate models")
    parser.add_argument("workspace_root", type=Path)
    parser.add_argument("--owner-unit", required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def load_spec(workspace_root: Path) -> dict:
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as fh:
        return tomllib.load(fh)


def repo_names(spec: dict) -> list[str]:
    return [repo["name"] for repo in spec.get("repos", [])]


def probe(workspace_root: Path, owner_unit: str) -> dict:
    spec = load_spec(workspace_root)
    repos = repo_names(spec)
    observations: list[dict[str, object]] = []
    shared_git_count = 0
    unit_git_count = 0

    for repo in repos:
        shared_root = workspace_root / "repos" / repo
        unit_root = workspace_root / "agents" / owner_unit / repo
        lane_root = workspace_root / "agents" / owner_unit / "lanes" / "feat-auth" / "repos" / repo

        shared_git = (shared_root / ".git").exists()
        unit_git = (unit_root / ".git").exists()
        lane_git = (lane_root / ".git").exists()
        shared_exists = shared_root.exists()
        unit_exists = unit_root.exists()

        if shared_git:
            shared_git_count += 1
        if unit_git:
            unit_git_count += 1

        observations.append(
            {
                "repo": repo,
                "shared_path_exists": shared_exists,
                "shared_git": shared_git,
                "unit_path_exists": unit_exists,
                "unit_git": unit_git,
                "lane_git": lane_git,
            }
        )

    shared_first_score = 0
    unit_first_score = 0
    reasons: list[str] = []

    if unit_git_count == len(repos):
        unit_first_score += 3
        reasons.append("all repos materialized as unit-local git checkouts")
    if shared_git_count == len(repos):
        shared_first_score += 3
        reasons.append("all repos materialized as shared git checkouts")
    if shared_git_count == 0 and any(item["shared_path_exists"] for item in observations):
        unit_first_score += 2
        reasons.append("shared repo paths exist as placeholders rather than active checkouts")
    if any(item["lane_git"] for item in observations):
        shared_first_score += 1
        unit_first_score += 1
        reasons.append("lane-local repo materialization exists, so a routed model may be emerging")
    else:
        unit_first_score += 1
        reasons.append("lane-local repos are not yet materialized")

    if unit_first_score > shared_first_score:
        recommendation = "ratify-unit-local-first"
        summary = (
            "Current behavior matches a unit-local-first model more closely than a shared-repo-first model."
        )
    elif shared_first_score > unit_first_score:
        recommendation = "push-shared-repo-model"
        summary = (
            "Current behavior already resembles a shared-repo-first model strongly enough to continue that direction."
        )
    else:
        recommendation = "hybrid-needs-clarification"
        summary = "Observed behavior is split enough that the model should be clarified explicitly."

    return {
        "owner_unit": owner_unit,
        "recommendation": recommendation,
        "summary": summary,
        "shared_first_score": shared_first_score,
        "unit_first_score": unit_first_score,
        "reasons": reasons,
        "observations": observations,
    }


def main() -> int:
    args = parse_args()
    result = probe(args.workspace_root.resolve(), args.owner_unit)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("gr2 prototype layout-model probe")
    print(f"owner: {result['owner_unit']}")
    print(f"recommendation: {result['recommendation']}")
    print(f"summary: {result['summary']}")
    print(f"score shared-first={result['shared_first_score']} unit-local-first={result['unit_first_score']}")
    print("reasons:")
    for reason in result["reasons"]:
        print(f"- {reason}")
    print("observations:")
    print("REPO\tSHARED_PATH\tSHARED_GIT\tUNIT_PATH\tUNIT_GIT\tLANE_GIT")
    for row in result["observations"]:
        print(
            f"{row['repo']}\t{str(row['shared_path_exists']).lower()}\t{str(row['shared_git']).lower()}\t"
            f"{str(row['unit_path_exists']).lower()}\t{str(row['unit_git']).lower()}\t{str(row['lane_git']).lower()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
