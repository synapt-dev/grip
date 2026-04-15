from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lane_proto

from .gitops import is_git_dir, is_git_repo, repo_dirty
from .hooks import load_repo_hooks
from .spec_apply import (
    ValidationIssue,
    load_workspace_spec_doc,
    repo_cache_path,
    validate_spec,
    workspace_spec_path,
)


@dataclasses.dataclass(frozen=True)
class SyncIssue:
    level: str
    code: str
    scope: str
    subject: str
    message: str
    blocks: bool
    path: str | None = None
    details: dict[str, object] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SyncOperation:
    kind: str
    scope: str
    subject: str
    target_path: str
    reason: str
    details: dict[str, object] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SyncPlan:
    workspace_root: str
    spec_path: str
    status: str
    issues: list[SyncIssue]
    operations: list[SyncOperation]

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_root": self.workspace_root,
            "spec_path": self.spec_path,
            "status": self.status,
            "issue_count": len(self.issues),
            "operation_count": len(self.operations),
            "issues": [item.as_dict() for item in self.issues],
            "operations": [item.as_dict() for item in self.operations],
        }


@dataclasses.dataclass(frozen=True)
class SyncResult:
    workspace_root: str
    status: str
    plan_status: str
    applied: list[str]
    blocked: list[SyncIssue]
    failures: list[SyncIssue]
    rollback_contract: str

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_root": self.workspace_root,
            "status": self.status,
            "plan_status": self.plan_status,
            "applied": list(self.applied),
            "blocked": [item.as_dict() for item in self.blocked],
            "failures": [item.as_dict() for item in self.failures],
            "rollback_contract": self.rollback_contract,
        }


def _spec_issue_to_sync(issue: ValidationIssue) -> SyncIssue:
    return SyncIssue(
        level=issue.level,
        code=issue.code,
        scope="workspace_spec",
        subject=issue.path or "workspace_spec",
        message=issue.message,
        blocks=issue.level == "error",
        path=issue.path,
    )


def _iter_lane_docs(workspace_root: Path) -> list[tuple[str, str, dict[str, object]]]:
    lanes_root = workspace_root / "agents"
    docs: list[tuple[str, str, dict[str, object]]] = []
    if not lanes_root.exists():
        return docs
    for owner_dir in sorted(lanes_root.iterdir()):
        lane_parent = owner_dir / "lanes"
        if not lane_parent.is_dir():
            continue
        for lane_dir in sorted(lane_parent.iterdir()):
            lane_toml = lane_dir / "lane.toml"
            if not lane_toml.exists():
                continue
            try:
                doc = lane_proto.load_lane_doc(workspace_root, owner_dir.name, lane_dir.name)
            except Exception as exc:  # pragma: no cover - defensive against prototype parser issues
                docs.append(
                    (
                        owner_dir.name,
                        lane_dir.name,
                        {
                            "lane_name": lane_dir.name,
                            "owner_unit": owner_dir.name,
                            "_load_error": str(exc),
                        },
                    )
                )
                continue
            docs.append((owner_dir.name, lane_dir.name, doc))
    return docs


def _status_from_issues(issues: list[SyncIssue]) -> str:
    if any(item.blocks for item in issues):
        return "blocked"
    if issues:
        return "attention"
    return "ready"


def build_sync_plan(workspace_root: Path) -> SyncPlan:
    workspace_root = workspace_root.resolve()
    spec_path = workspace_spec_path(workspace_root)
    if not spec_path.exists():
        raise SystemExit(
            f"workspace spec not found: {spec_path}\n"
            "run `gr2 workspace init <path>` first or create .grip/workspace_spec.toml explicitly"
        )

    issues: list[SyncIssue] = []
    operations: list[SyncOperation] = []

    issues.extend(_spec_issue_to_sync(issue) for issue in validate_spec(workspace_root))
    if any(item.blocks for item in issues):
        return SyncPlan(
            workspace_root=str(workspace_root),
            spec_path=str(spec_path),
            status=_status_from_issues(issues),
            issues=issues,
            operations=operations,
        )

    spec = load_workspace_spec_doc(workspace_root)
    for repo in spec.get("repos", []):
        repo_name = str(repo["name"])
        repo_root = workspace_root / str(repo["path"])
        cache_root = repo_cache_path(workspace_root, repo_name)

        if not cache_root.exists():
            operations.append(
                SyncOperation(
                    kind="seed_repo_cache",
                    scope="repo_cache",
                    subject=repo_name,
                    target_path=str(cache_root),
                    reason="shared repo cache missing",
                    details={"url": str(repo["url"])},
                )
            )
        elif not is_git_dir(cache_root):
            issues.append(
                SyncIssue(
                    level="error",
                    code="cache_path_conflict",
                    scope="repo_cache",
                    subject=repo_name,
                    message=f"repo cache path exists but is not a bare git dir: {cache_root}",
                    blocks=True,
                    path=str(cache_root),
                )
            )
        else:
            operations.append(
                SyncOperation(
                    kind="refresh_repo_cache",
                    scope="repo_cache",
                    subject=repo_name,
                    target_path=str(cache_root),
                    reason="shared repo cache present; refresh remote state",
                    details={"url": str(repo["url"])},
                )
            )

        if not repo_root.exists():
            operations.append(
                SyncOperation(
                    kind="clone_shared_repo",
                    scope="shared_repo",
                    subject=repo_name,
                    target_path=str(repo_root),
                    reason="shared repo checkout missing",
                    details={"url": str(repo["url"])},
                )
            )
        elif not is_git_repo(repo_root):
            issues.append(
                SyncIssue(
                    level="error",
                    code="shared_repo_path_conflict",
                    scope="shared_repo",
                    subject=repo_name,
                    message=f"shared repo path exists but is not a git repo: {repo_root}",
                    blocks=True,
                    path=str(repo_root),
                )
            )
        else:
            if repo_dirty(repo_root):
                issues.append(
                    SyncIssue(
                        level="error",
                        code="dirty_shared_repo",
                        scope="shared_repo",
                        subject=repo_name,
                        message=f"shared repo has uncommitted changes and blocks sync: {repo_root}",
                        blocks=True,
                        path=str(repo_root),
                    )
                )
            hooks = load_repo_hooks(repo_root)
            if hooks:
                operations.append(
                    SyncOperation(
                        kind="evaluate_repo_hooks",
                        scope="shared_repo",
                        subject=repo_name,
                        target_path=str(repo_root),
                        reason="repo hook config present; sync must account for lifecycle/policy rules",
                        details={"hook_config": str(repo_root / ".gr2" / "hooks.toml")},
                    )
                )

    for owner_unit, lane_name, lane_doc in _iter_lane_docs(workspace_root):
        if lane_doc.get("_load_error"):
            issues.append(
                SyncIssue(
                    level="error",
                    code="lane_doc_load_failed",
                    scope="lane",
                    subject=f"{owner_unit}/{lane_name}",
                    message=f"failed to load lane metadata: {lane_doc['_load_error']}",
                    blocks=True,
                    path=str(workspace_root / "agents" / owner_unit / "lanes" / lane_name / "lane.toml"),
                )
            )
            continue

        lane_root = lane_proto.lane_dir(workspace_root, owner_unit, lane_name)
        for repo_name in lane_doc.get("repos", []):
            lane_repo_root = lane_root / "repos" / str(repo_name)
            expected_branch = str(dict(lane_doc.get("branch_map", {})).get(repo_name, ""))
            if not lane_repo_root.exists():
                operations.append(
                    SyncOperation(
                        kind="materialize_lane_repo",
                        scope="lane",
                        subject=f"{owner_unit}/{lane_name}:{repo_name}",
                        target_path=str(lane_repo_root),
                        reason="lane checkout missing",
                        details={"expected_branch": expected_branch},
                    )
                )
                continue
            if not is_git_repo(lane_repo_root):
                issues.append(
                    SyncIssue(
                        level="error",
                        code="lane_repo_path_conflict",
                        scope="lane",
                        subject=f"{owner_unit}/{lane_name}:{repo_name}",
                        message=f"lane repo path exists but is not a git repo: {lane_repo_root}",
                        blocks=True,
                        path=str(lane_repo_root),
                    )
                )
                continue
            if repo_dirty(lane_repo_root):
                issues.append(
                    SyncIssue(
                        level="error",
                        code="dirty_lane_repo",
                        scope="lane",
                        subject=f"{owner_unit}/{lane_name}:{repo_name}",
                        message=f"lane repo has uncommitted changes and blocks sync: {lane_repo_root}",
                        blocks=True,
                        path=str(lane_repo_root),
                        details={"expected_branch": expected_branch},
                    )
                )
            operations.append(
                SyncOperation(
                    kind="inspect_lane_repo_branch",
                    scope="lane",
                    subject=f"{owner_unit}/{lane_name}:{repo_name}",
                    target_path=str(lane_repo_root),
                    reason="lane checkout present; verify branch alignment before any sync run",
                    details={"expected_branch": expected_branch},
                )
            )

    return SyncPlan(
        workspace_root=str(workspace_root),
        spec_path=str(spec_path),
        status=_status_from_issues(issues),
        issues=issues,
        operations=operations,
    )


def render_sync_plan(plan: SyncPlan) -> str:
    lines = [
        "SyncPlan",
        f"workspace_root = {plan.workspace_root}",
        f"status = {plan.status}",
        f"issue_count = {len(plan.issues)}",
        f"operation_count = {len(plan.operations)}",
    ]
    if plan.issues:
        lines.append("ISSUES")
        for issue in plan.issues:
            subject = f" [{issue.subject}]" if issue.subject else ""
            lines.append(f"- {issue.level}:{issue.code}{subject} {issue.message}")
    if plan.operations:
        lines.append("OPERATIONS")
        for op in plan.operations:
            lines.append(f"- {op.kind} [{op.scope}] {op.subject} -> {op.target_path} ({op.reason})")
    return "\n".join(lines)


def sync_status_payload(workspace_root: Path) -> dict[str, object]:
    return build_sync_plan(workspace_root).as_dict()


def sync_status_json(workspace_root: Path) -> str:
    return json.dumps(sync_status_payload(workspace_root), indent=2)
