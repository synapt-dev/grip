from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lane_proto


def _resolve_lane_name(workspace_root: Path, owner_unit: str, lane_name: str | None) -> str:
    if lane_name:
        return lane_name
    current = lane_proto.load_current_lane_doc(workspace_root, owner_unit)
    return str(current["current"]["lane_name"])


def _selected_repos(lane_doc: dict[str, object], repos: str | None) -> list[str]:
    selected = [str(repo) for repo in lane_doc.get("repos", [])]
    if repos:
        requested = set(lane_proto.parse_repo_list(repos))
        selected = [repo for repo in selected if repo in requested]
    return selected


def _blocked_payload(
    *,
    reason: str,
    lane_doc: dict[str, object],
    owner_unit: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {
        "status": "blocked",
        "reason": reason,
        "lane": str(lane_doc["lane_name"]),
        "owner_unit": owner_unit,
    }
    if extra:
        payload.update(extra)
    return payload


def exec_status_payload(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str | None,
    *,
    repos: str | None = None,
    actor: str = "agent:exec-status",
) -> dict[str, object]:
    owner_unit = str(owner_unit)
    lane_name = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)

    rebind_doc = lane_proto.load_unit_rebind_doc(workspace_root, owner_unit)
    if rebind_doc:
        affected = {item["lane_name"]: item for item in rebind_doc.get("affected_lanes", [])}
        if lane_name in affected:
            return _blocked_payload(
                reason="unit-rebound",
                lane_doc=lane_doc,
                owner_unit=owner_unit,
                extra={
                    "new_owner_unit": rebind_doc["new_owner_unit"],
                    "hint": "create a continuation lane under the new unit before resuming work",
                },
            )

    leases = lane_proto.load_lane_leases(workspace_root, owner_unit, lane_name)
    active_conflicts, stale_conflicts = lane_proto.conflicting_leases(leases, actor, "exec")
    if active_conflicts:
        return _blocked_payload(
            reason="conflicting-active-lease",
            lane_doc=lane_doc,
            owner_unit=owner_unit,
            extra={"requested_mode": "exec", "conflicting_leases": active_conflicts},
        )
    if stale_conflicts:
        return _blocked_payload(
            reason="stale-conflicting-lease",
            lane_doc=lane_doc,
            owner_unit=owner_unit,
            extra={
                "requested_mode": "exec",
                "conflicting_leases": stale_conflicts,
                "hint": "break stale leases with gr2 lane lease acquire --force or clean them up first",
            },
        )

    selected_repos = _selected_repos(lane_doc, repos)
    rows: list[dict[str, object]] = []
    for repo in selected_repos:
        cwd = workspace_root / "agents" / owner_unit / "lanes" / lane_name / "repos" / repo
        rows.append(
            {
                "lane": lane_name,
                "owner_unit": owner_unit,
                "repo": repo,
                "branch": dict(lane_doc.get("branch_map", {})).get(repo),
                "cwd": str(cwd),
                "exists": cwd.exists(),
                "shared_context_roots": lane_doc.get("context", {}).get("shared_roots", []),
                "private_context_roots": lane_doc.get("context", {}).get("private_roots", []),
                "parallelism": lane_doc["exec_defaults"].get("parallelism"),
                "fail_fast": bool(lane_doc["exec_defaults"].get("fail_fast", True)),
                "default_command_family": lane_doc["exec_defaults"].get("default_command_family", []),
                "commands": lane_doc["exec_defaults"].get("commands", []),
            }
        )

    return {
        "status": "ready",
        "lane": lane_name,
        "owner_unit": owner_unit,
        "lane_type": lane_doc["lane_type"],
        "rows": rows,
    }


def render_exec_status(payload: dict[str, object]) -> str:
    if payload["status"] != "ready":
        lines = [
            "ExecStatus",
            f"status = {payload['status']}",
            f"reason = {payload['reason']}",
            f"owner_unit = {payload['owner_unit']}",
            f"lane = {payload['lane']}",
        ]
        if "hint" in payload:
            lines.append(f"hint = {payload['hint']}")
        return "\n".join(lines)

    lines = [
        "ExecStatus",
        f"status = {payload['status']}",
        f"owner_unit = {payload['owner_unit']}",
        f"lane = {payload['lane']}",
        f"lane_type = {payload['lane_type']}",
        "REPO\tBRANCH\tEXISTS\tCWD",
    ]
    for row in payload["rows"]:
        lines.append(f"{row['repo']}\t{row['branch']}\t{row['exists']}\t{row['cwd']}")
    return "\n".join(lines)


def _emit_lease_event(
    workspace_root: Path,
    *,
    owner_unit: str,
    lane_name: str,
    actor: str,
    event_type: str,
    ttl_seconds: int | None = None,
) -> None:
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    unit_spec = lane_proto.find_unit_spec(workspace_root, owner_unit)
    payload = {
        "type": event_type,
        "agent": actor,
        "agent_id": unit_spec.get("agent_id"),
        "owner_unit": owner_unit,
        "lane": lane_name,
        "lane_type": lane_doc["lane_type"],
        "repos": lane_doc.get("repos", []),
        "timestamp": lane_proto.now_utc(),
    }
    if ttl_seconds is not None:
        payload["lease_mode"] = "exec"
        payload["ttl_seconds"] = ttl_seconds
    lane_proto.emit_lane_event(workspace_root, payload)


def acquire_exec_lease(workspace_root: Path, owner_unit: str, lane_name: str, actor: str, ttl_seconds: int) -> None:
    def mutator(leases: list[dict]) -> dict:
        retained = [lease for lease in leases if lease["actor"] != actor]
        active_conflicts, stale_conflicts = lane_proto.conflicting_leases(retained, actor, "exec")
        if active_conflicts:
            return {"status": "blocked", "payload": {"reason": "conflicting-active-lease", "conflicting_leases": active_conflicts}, "write": False}
        if stale_conflicts:
            return {"status": "blocked", "payload": {"reason": "stale-conflicting-lease", "conflicting_leases": stale_conflicts}, "write": False}
        retained.append(lane_proto.build_lease(actor, "exec", ttl_seconds))
        return {"status": "ok", "leases": retained, "write": True}

    result = lane_proto.mutate_lane_leases(workspace_root, owner_unit, lane_name, mutator)
    if result["status"] == "blocked":
        raise SystemExit(json.dumps(result["payload"], indent=2))
    _emit_lease_event(
        workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
        event_type="lease_acquire",
        ttl_seconds=ttl_seconds,
    )


def release_exec_lease(workspace_root: Path, owner_unit: str, lane_name: str, actor: str) -> None:
    lane_proto.mutate_lane_leases(
        workspace_root,
        owner_unit,
        lane_name,
        lambda leases: {
            "status": "ok",
            "leases": [lease for lease in leases if lease["actor"] != actor],
            "write": True,
        },
    )
    _emit_lease_event(
        workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
        event_type="lease_release",
    )


def run_exec(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str | None,
    *,
    actor: str,
    command: list[str],
    repos: str | None = None,
    ttl_seconds: int = 900,
) -> dict[str, object]:
    status = exec_status_payload(workspace_root, owner_unit, lane_name, repos=repos, actor=actor)
    if status["status"] != "ready":
        return status

    resolved_lane = str(status["lane"])
    rows = list(status["rows"])
    fail_fast = bool(rows[0]["fail_fast"]) if rows else True
    acquire_exec_lease(workspace_root, owner_unit, resolved_lane, actor, ttl_seconds)
    results: list[dict[str, object]] = []
    overall = "success"

    try:
        for row in rows:
            cwd = Path(str(row["cwd"]))
            if not cwd.exists():
                result = {
                    "repo": row["repo"],
                    "cwd": str(cwd),
                    "status": "missing",
                    "returncode": None,
                    "stdout": "",
                    "stderr": f"lane repo checkout missing: {cwd}",
                }
                results.append(result)
                overall = "failed"
                if fail_fast:
                    break
                continue

            proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
            result = {
                "repo": row["repo"],
                "cwd": str(cwd),
                "status": "ok" if proc.returncode == 0 else "failed",
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            results.append(result)
            if proc.returncode != 0:
                overall = "failed"
                if fail_fast:
                    break
    finally:
        release_exec_lease(workspace_root, owner_unit, resolved_lane, actor)

    return {
        "status": overall,
        "owner_unit": owner_unit,
        "lane": resolved_lane,
        "command": command,
        "results": results,
    }


def render_exec_run(payload: dict[str, object]) -> str:
    if payload.get("status") == "blocked":
        return render_exec_status(payload)
    lines = [
        "ExecRun",
        f"status = {payload['status']}",
        f"owner_unit = {payload['owner_unit']}",
        f"lane = {payload['lane']}",
        f"command = {' '.join(payload['command'])}",
        "REPO\tSTATUS\tRETURNCODE\tCWD",
    ]
    for result in payload["results"]:
        lines.append(
            f"{result['repo']}\t{result['status']}\t{result['returncode']}\t{result['cwd']}"
        )
    return "\n".join(lines)
