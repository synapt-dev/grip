from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import time
from pathlib import Path
from datetime import UTC, datetime

from gr2.prototypes import lane_workspace_prototype as lane_proto

from .gitops import (
    clone_repo,
    commits_between,
    current_branch,
    current_head_sha,
    discard_if_dirty,
    ensure_lane_checkout,
    ensure_repo_cache,
    is_git_dir,
    is_git_repo,
    repo_dirty,
    stash_if_dirty,
)
from .hooks import load_repo_hooks
from .spec_apply import (
    ValidationIssue,
    _run_materialize_hooks,
    _find_repo,
    _record_apply_state,
    load_workspace_spec_doc,
    repo_cache_path,
    validate_spec,
    workspace_spec_path,
)


SYNC_ROLLBACK_CONTRACT = (
    "sync preserves completed operations, stops on blocking failure, and reports partial state explicitly; "
    "it does not attempt automatic cross-repo rollback"
)
VALID_DIRTY_MODES = {"stash", "block", "discard"}
SYNC_STRATEGY = "reference-cache"


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
    dirty_mode: str
    dirty_targets: list[str]
    issues: list[SyncIssue]
    operations: list[SyncOperation]

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_root": self.workspace_root,
            "spec_path": self.spec_path,
            "status": self.status,
            "dirty_mode": self.dirty_mode,
            "dirty_targets": list(self.dirty_targets),
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
    dirty_mode: str
    dirty_targets: list[str]
    applied: list[str]
    blocked: list[SyncIssue]
    failures: list[SyncIssue]
    rollback_contract: str
    operation_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_root": self.workspace_root,
            "status": self.status,
            "plan_status": self.plan_status,
            "dirty_mode": self.dirty_mode,
            "dirty_targets": list(self.dirty_targets),
            "applied": list(self.applied),
            "blocked": [item.as_dict() for item in self.blocked],
            "failures": [item.as_dict() for item in self.failures],
            "rollback_contract": self.rollback_contract,
            "operation_id": self.operation_id,
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


def _dirty_targets(issues: list[SyncIssue], operations: list[SyncOperation]) -> list[str]:
    targets: list[str] = []
    for issue in issues:
        if issue.code in {"dirty_shared_repo", "dirty_lane_repo"}:
            targets.append(issue.subject)
    for op in operations:
        if op.kind in {"stash_dirty_repo", "discard_dirty_repo"}:
            targets.append(op.subject)
    return sorted(dict.fromkeys(targets))


def _normalize_dirty_mode(dirty_mode: str) -> str:
    normalized = dirty_mode.strip().lower()
    if normalized not in VALID_DIRTY_MODES:
        raise SystemExit(f"invalid --dirty value '{dirty_mode}'; expected one of: stash, block, discard")
    return normalized


def _operation_id() -> str:
    return os.urandom(8).hex()


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def _outbox_file(workspace_root: Path) -> Path:
    return _events_dir(workspace_root) / "outbox.jsonl"


def _outbox_lock_file(workspace_root: Path) -> Path:
    return _events_dir(workspace_root) / "outbox.lock"


def _sync_lock_file(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "state" / "sync.lock"


def _append_outbox_event(workspace_root: Path, payload: dict[str, object]) -> None:
    outbox_path = _outbox_file(workspace_root)
    lock_path = _outbox_lock_file(workspace_root)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            seq = 1
            if outbox_path.exists():
                with outbox_path.open("r", encoding="utf-8") as existing:
                    for line in existing:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        value = int(row.get("seq", 0))
                        if value >= seq:
                            seq = value + 1
            event = {
                "seq": seq,
                "event_id": os.urandom(8).hex(),
                "timestamp": _now_utc(),
                **payload,
            }
            with outbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return


def _emit_sync_event(workspace_root: Path, payload: dict[str, object]) -> None:
    _append_outbox_event(workspace_root, payload)


def _plan_repo_names(plan: SyncPlan) -> list[str]:
    repo_names: list[str] = []
    for op in plan.operations:
        if op.scope in {"shared_repo", "lane"}:
            repo_names.append(op.subject.split(":")[-1])
    return sorted(dict.fromkeys(repo_names))


def build_sync_plan(workspace_root: Path, *, dirty_mode: str = "stash") -> SyncPlan:
    workspace_root = workspace_root.resolve()
    dirty_mode = _normalize_dirty_mode(dirty_mode)
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
            dirty_mode=dirty_mode,
            dirty_targets=[],
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
                if dirty_mode == "block":
                    issues.append(
                        SyncIssue(
                            level="error",
                            code="dirty_shared_repo",
                            scope="shared_repo",
                            subject=repo_name,
                            message=f"shared repo has uncommitted changes and blocks sync: {repo_root}",
                            blocks=True,
                            path=str(repo_root),
                            details={"dirty_mode": dirty_mode},
                        )
                    )
                else:
                    operations.append(
                        SyncOperation(
                            kind="stash_dirty_repo" if dirty_mode == "stash" else "discard_dirty_repo",
                            scope="shared_repo",
                            subject=repo_name,
                            target_path=str(repo_root),
                            reason=f"shared repo is dirty and will be handled via --dirty={dirty_mode}",
                            details={"dirty_mode": dirty_mode},
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
        active_leases = [
            lease
            for lease in lane_proto.load_lane_leases(workspace_root, owner_unit, lane_name)
            if not lane_proto.is_stale_lease(lease)
        ]
        if active_leases:
            issues.append(
                SyncIssue(
                    level="error",
                    code="lease_blocked_sync",
                    scope="lane",
                    subject=f"{owner_unit}/{lane_name}",
                    message=f"lane has active leases that block sync mutation: {owner_unit}/{lane_name}",
                    blocks=True,
                    path=str(workspace_root / "agents" / owner_unit / "lanes" / lane_name),
                    details={
                        "leases": [
                            {"actor": lease["actor"], "mode": lease["mode"], "acquired_at": lease["acquired_at"]}
                            for lease in active_leases
                        ]
                    },
                )
            )

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
                if dirty_mode == "block":
                    issues.append(
                        SyncIssue(
                            level="error",
                            code="dirty_lane_repo",
                            scope="lane",
                            subject=f"{owner_unit}/{lane_name}:{repo_name}",
                            message=f"lane repo has uncommitted changes and blocks sync: {lane_repo_root}",
                            blocks=True,
                            path=str(lane_repo_root),
                            details={"expected_branch": expected_branch, "dirty_mode": dirty_mode},
                        )
                    )
                else:
                    operations.append(
                        SyncOperation(
                            kind="stash_dirty_repo" if dirty_mode == "stash" else "discard_dirty_repo",
                            scope="lane",
                            subject=f"{owner_unit}/{lane_name}:{repo_name}",
                            target_path=str(lane_repo_root),
                            reason=f"lane repo is dirty and will be handled via --dirty={dirty_mode}",
                            details={"expected_branch": expected_branch, "dirty_mode": dirty_mode},
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
        dirty_mode=dirty_mode,
        dirty_targets=_dirty_targets(issues, operations),
        issues=issues,
        operations=operations,
    )


def render_sync_plan(plan: SyncPlan) -> str:
    lines = [
        "SyncPlan",
        f"workspace_root = {plan.workspace_root}",
        f"status = {plan.status}",
        f"dirty_mode = {plan.dirty_mode}",
        f"issue_count = {len(plan.issues)}",
        f"operation_count = {len(plan.operations)}",
    ]
    if plan.dirty_targets:
        lines.append("DIRTY_TARGETS")
        lines.extend(f"- {item}" for item in plan.dirty_targets)
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


def _issue_from_exception(op: SyncOperation, exc: BaseException) -> SyncIssue:
    message = str(exc).strip() or f"sync operation failed: {op.kind}"
    return SyncIssue(
        level="error",
        code=f"{op.kind}_failed",
        scope=op.scope,
        subject=op.subject,
        message=message,
        blocks=True,
        path=op.target_path,
        details={"operation": op.kind},
    )


def _execute_operation(workspace_root: Path, spec: dict[str, object], op: SyncOperation) -> str:
    target_path = Path(op.target_path)
    before_sha = current_head_sha(target_path) if op.scope in {"shared_repo", "lane"} and target_path.exists() else None
    if op.kind in {"seed_repo_cache", "refresh_repo_cache"}:
        repo_spec = _find_repo(spec, op.subject)
        cache_path = repo_cache_path(workspace_root, str(repo_spec["name"]))
        created = ensure_repo_cache(str(repo_spec["url"]), cache_path)
        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.cache_seeded" if created else "sync.cache_refreshed",
                "repo": op.subject,
                "strategy": SYNC_STRATEGY,
                "cache_path": str(cache_path),
            },
        )
        if op.kind == "seed_repo_cache":
            return f"seeded repo cache for '{op.subject}' at {cache_path}"
        if created:
            return f"seeded repo cache for '{op.subject}' at {cache_path}"
        return f"refreshed repo cache for '{op.subject}' at {cache_path}"

    if op.kind == "clone_shared_repo":
        repo_spec = _find_repo(spec, op.subject)
        repo_root = workspace_root / str(repo_spec["path"])
        cache_path = repo_cache_path(workspace_root, str(repo_spec["name"]))
        first_materialize = clone_repo(str(repo_spec["url"]), repo_root, reference_repo_root=cache_path)
        _run_materialize_hooks(workspace_root, repo_root, str(repo_spec["name"]), first_materialize, manual_hooks=False)
        after_sha = current_head_sha(repo_root)
        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.repo_updated",
                "repo": op.subject,
                "scope": "shared_repo",
                "old_sha": before_sha,
                "new_sha": after_sha,
                "strategy": SYNC_STRATEGY,
                "commits_pulled": commits_between(repo_root, before_sha, after_sha),
            },
        )
        return f"cloned shared repo '{op.subject}' into {repo_root}"

    if op.kind == "evaluate_repo_hooks":
        repo_root = Path(op.target_path)
        hooks = load_repo_hooks(repo_root)
        if hooks:
            return f"validated repo hooks for '{op.subject}'"
        return f"no repo hooks for '{op.subject}'"

    if op.kind == "materialize_lane_repo":
        owner_and_lane, repo_name = op.subject.split(":", 1)
        owner_unit, lane_name = owner_and_lane.split("/", 1)
        repo_spec = _find_repo(spec, repo_name)
        source_repo_root = workspace_root / str(repo_spec["path"])
        target_repo_root = Path(op.target_path)
        expected_branch = str(op.details.get("expected_branch", ""))
        first_materialize = ensure_lane_checkout(
            source_repo_root=source_repo_root,
            target_repo_root=target_repo_root,
            branch=expected_branch,
        )
        _run_materialize_hooks(workspace_root, target_repo_root, repo_name, first_materialize, manual_hooks=False)
        after_sha = current_head_sha(target_repo_root)
        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.repo_updated",
                "repo": repo_name,
                "scope": "lane",
                "owner_unit": owner_unit,
                "lane": lane_name,
                "old_sha": before_sha,
                "new_sha": after_sha,
                "branch": expected_branch,
                "strategy": SYNC_STRATEGY,
                "commits_pulled": commits_between(target_repo_root, before_sha, after_sha),
            },
        )
        return f"materialized lane repo '{op.subject}' at {target_repo_root}"

    if op.kind == "inspect_lane_repo_branch":
        expected_branch = str(op.details.get("expected_branch", "")).strip()
        repo_root = Path(op.target_path)
        actual_branch = current_branch(repo_root)
        if expected_branch and actual_branch != expected_branch:
            raise SystemExit(
                f"lane repo branch mismatch for {op.subject}: expected {expected_branch}, found {actual_branch}"
            )
        return f"verified lane branch for '{op.subject}' ({actual_branch or '-'})"

    if op.kind == "stash_dirty_repo":
        repo_root = Path(op.target_path)
        if stash_if_dirty(repo_root, f"gr2 sync auto-stash: {op.subject}"):
            _emit_sync_event(
                workspace_root,
                {
                    "type": "sync.repo_skipped",
                    "repo": op.subject.split(":")[-1],
                    "scope": op.scope,
                    "reason": "dirty_stashed",
                },
            )
            return f"stashed dirty repo state for '{op.subject}'"
        return f"repo already clean for '{op.subject}'"

    if op.kind == "discard_dirty_repo":
        repo_root = Path(op.target_path)
        if discard_if_dirty(repo_root):
            _emit_sync_event(
                workspace_root,
                {
                    "type": "sync.repo_skipped",
                    "repo": op.subject.split(":")[-1],
                    "scope": op.scope,
                    "reason": "dirty_discarded",
                },
            )
            return f"discarded dirty repo state for '{op.subject}'"
        return f"repo already clean for '{op.subject}'"

    raise SystemExit(f"unsupported sync operation kind: {op.kind}")


def _acquire_sync_lock(workspace_root: Path):
    lock_path = _sync_lock_file(workspace_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        return None
    return lock_fh


def _release_sync_lock(lock_fh) -> None:
    if lock_fh is None:
        return
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()


def run_sync(workspace_root: Path, *, dirty_mode: str = "stash") -> SyncResult:
    workspace_root = workspace_root.resolve()
    dirty_mode = _normalize_dirty_mode(dirty_mode)
    operation_id = _operation_id()
    started_at = time.monotonic()
    lock_fh = _acquire_sync_lock(workspace_root)
    if lock_fh is None:
        blocked_issue = SyncIssue(
            level="error",
            code="sync_lock_held",
            scope="workspace",
            subject=str(workspace_root),
            message="another sync run currently holds the workspace lock",
            blocks=True,
            path=str(_sync_lock_file(workspace_root)),
            details={"operation_id": operation_id},
        )
        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.conflict",
                "operation_id": operation_id,
                "reason": "lock_held",
                "workspace_root": str(workspace_root),
            },
        )
        return SyncResult(
            workspace_root=str(workspace_root),
            status="blocked",
            plan_status="blocked",
            dirty_mode=dirty_mode,
            dirty_targets=[],
            applied=[],
            blocked=[blocked_issue],
            failures=[],
            rollback_contract=SYNC_ROLLBACK_CONTRACT,
            operation_id=operation_id,
        )

    _emit_sync_event(
        workspace_root,
        {
            "type": "sync.started",
            "operation_id": operation_id,
            "workspace_root": str(workspace_root),
            "dirty_mode": dirty_mode,
            "repos": _plan_repo_names(build_sync_plan(workspace_root, dirty_mode=dirty_mode)),
            "strategy": SYNC_STRATEGY,
        },
    )
    plan = build_sync_plan(workspace_root, dirty_mode=dirty_mode)
    blocked = [issue for issue in plan.issues if issue.blocks]
    if blocked:
        for issue in blocked:
            if issue.code == "lease_blocked_sync":
                _emit_sync_event(
                    workspace_root,
                    {
                        "type": "sync.conflict",
                        "operation_id": operation_id,
                        "workspace_root": str(workspace_root),
                        "reason": "active_lease",
                        "subject": issue.subject,
                        "leases": issue.details.get("leases", []),
                    },
                )
        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.completed",
                "operation_id": operation_id,
                "workspace_root": str(workspace_root),
                "status": "blocked",
                "blocked_codes": [item.code for item in blocked],
                "repos_updated": 0,
                "repos_skipped": 0,
                "repos_failed": len(blocked),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        _release_sync_lock(lock_fh)
        return SyncResult(
            workspace_root=str(workspace_root),
            status="blocked",
            plan_status=plan.status,
            dirty_mode=dirty_mode,
            dirty_targets=list(plan.dirty_targets),
            applied=[],
            blocked=blocked,
            failures=[],
            rollback_contract=SYNC_ROLLBACK_CONTRACT,
            operation_id=operation_id,
        )

    spec = load_workspace_spec_doc(workspace_root)
    applied: list[str] = []
    failures: list[SyncIssue] = []
    try:
        for op in plan.operations:
            try:
                applied.append(_execute_operation(workspace_root, spec, op))
            except Exception as exc:
                failures.append(_issue_from_exception(op, exc))
                break

        if applied:
            _record_apply_state(workspace_root, applied)

        status = "success"
        if failures and applied:
            status = "partial_failure"
        elif failures:
            status = "failed"

        _emit_sync_event(
            workspace_root,
            {
                "type": "sync.completed",
                "operation_id": operation_id,
                "workspace_root": str(workspace_root),
                "status": status,
                "applied_count": len(applied),
                "failure_codes": [item.code for item in failures],
                "repos_updated": sum(1 for op in plan.operations if op.kind in {"clone_shared_repo", "materialize_lane_repo"}),
                "repos_skipped": sum(1 for op in plan.operations if op.kind in {"stash_dirty_repo", "discard_dirty_repo"}),
                "repos_failed": len(failures),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )

        return SyncResult(
            workspace_root=str(workspace_root),
            status=status,
            plan_status=plan.status,
            dirty_mode=dirty_mode,
            dirty_targets=list(plan.dirty_targets),
            applied=applied,
            blocked=[],
            failures=failures,
            rollback_contract=SYNC_ROLLBACK_CONTRACT,
            operation_id=operation_id,
        )
    finally:
        _release_sync_lock(lock_fh)


def render_sync_result(result: SyncResult) -> str:
    lines = [
        "SyncResult",
        f"workspace_root = {result.workspace_root}",
        f"status = {result.status}",
        f"plan_status = {result.plan_status}",
        f"dirty_mode = {result.dirty_mode}",
        f"operation_id = {result.operation_id or '-'}",
        f"applied_count = {len(result.applied)}",
        f"failure_count = {len(result.failures)}",
    ]
    if result.dirty_targets:
        lines.append("DIRTY_TARGETS")
        lines.extend(f"- {item}" for item in result.dirty_targets)
    if result.applied:
        lines.append("APPLIED")
        lines.extend(f"- {item}" for item in result.applied)
    if result.blocked:
        lines.append("BLOCKED")
        lines.extend(f"- {item.code}: {item.message}" for item in result.blocked)
    if result.failures:
        lines.append("FAILURES")
        lines.extend(f"- {item.code}: {item.message}" for item in result.failures)
    lines.append(f"rollback_contract = {result.rollback_contract}")
    return "\n".join(lines)
