"""Tests for hook execution event emission.

Verifies that run_lifecycle_stage emits hook.started, hook.completed,
hook.failed, and hook.skipped events per HOOK-EVENT-CONTRACT.md sections
3.2 (Hook Execution) and 6.2-6.4.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli.hooks import (
    HookContext,
    HookRuntimeError,
    LifecycleHook,
    RepoHooks,
    run_lifecycle_stage,
)


def _make_ctx(workspace: Path) -> HookContext:
    repo_root = workspace / "repos" / "grip"
    repo_root.mkdir(parents=True, exist_ok=True)
    return HookContext(
        workspace_root=workspace,
        lane_root=workspace / "lanes" / "apollo" / "feat-test",
        repo_root=repo_root,
        repo_name="grip",
        lane_owner="apollo",
        lane_subject="grip",
        lane_name="feat/test",
    )


def _make_hooks(lifecycle_hooks: list[LifecycleHook], stage: str = "on_enter") -> RepoHooks:
    kwargs = {"on_materialize": [], "on_enter": [], "on_exit": []}
    kwargs[stage] = lifecycle_hooks
    return RepoHooks(
        repo_name="grip",
        file_links=[],
        file_copies=[],
        policy={},
        path=Path("/fake/.gr2/hooks.toml"),
        **kwargs,
    )


def _read_outbox(workspace: Path) -> list[dict]:
    outbox = workspace / ".grip" / "events" / "outbox.jsonl"
    if not outbox.exists():
        return []
    lines = outbox.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# 1. hook.completed (successful hook)
# ---------------------------------------------------------------------------

class TestHookCompleted:

    def test_emits_started_and_completed(self, workspace: Path):
        """Successful hook emits hook.started then hook.completed."""
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="check-version", command="true",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == ["hook.started", "hook.completed"]

    def test_started_payload(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="check-version", command="echo hello",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        started = events[0]
        assert started["type"] == "hook.started"
        assert started["stage"] == "on_enter"
        assert started["hook_name"] == "check-version"
        assert started["repo"] == "grip"
        assert "command" in started
        assert "cwd" in started

    def test_completed_payload(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="check-version", command="true",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        completed = events[1]
        assert completed["type"] == "hook.completed"
        assert completed["stage"] == "on_enter"
        assert completed["hook_name"] == "check-version"
        assert completed["repo"] == "grip"
        assert completed["exit_code"] == 0
        assert "duration_ms" in completed
        assert isinstance(completed["duration_ms"], int)


# ---------------------------------------------------------------------------
# 2. hook.failed with on_failure="block"
# ---------------------------------------------------------------------------

class TestHookFailedBlock:

    def test_emits_started_and_failed(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="install-deps", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks([hook])
        with pytest.raises(HookRuntimeError):
            run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == ["hook.started", "hook.failed"]

    def test_failed_payload(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="install-deps", command="echo bad >&2; false",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks([hook])
        with pytest.raises(HookRuntimeError):
            run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        failed = events[1]
        assert failed["type"] == "hook.failed"
        assert failed["stage"] == "on_enter"
        assert failed["hook_name"] == "install-deps"
        assert failed["repo"] == "grip"
        assert failed["exit_code"] != 0
        assert failed["on_failure"] == "block"
        assert "duration_ms" in failed
        assert "stderr_tail" in failed


# ---------------------------------------------------------------------------
# 3. hook.failed with on_failure="warn"
# ---------------------------------------------------------------------------

class TestHookFailedWarn:

    def test_emits_started_and_failed(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="lint", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="warn",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == ["hook.started", "hook.failed"]

    def test_failed_payload_on_failure_warn(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="lint", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="warn",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        failed = events[1]
        assert failed["on_failure"] == "warn"


# ---------------------------------------------------------------------------
# 4. hook.failed with on_failure="skip"
# ---------------------------------------------------------------------------

class TestHookFailedSkip:

    def test_emits_started_and_failed(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="optional", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="skip",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == ["hook.started", "hook.failed"]

    def test_failed_payload_on_failure_skip(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="optional", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="skip",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        failed = events[1]
        assert failed["on_failure"] == "skip"


# ---------------------------------------------------------------------------
# 5. hook.skipped (when condition not met)
# ---------------------------------------------------------------------------

class TestHookSkipped:

    def test_emits_skipped(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="first-only", command="true",
            cwd=str(ctx.repo_root), when="first_materialize", on_failure="block",
        )
        hooks = _make_hooks([hook])
        # first_materialize=False -> when=first_materialize does not match
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        assert len(events) == 1
        assert events[0]["type"] == "hook.skipped"

    def test_skipped_payload(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="first-only", command="true",
            cwd=str(ctx.repo_root), when="first_materialize", on_failure="block",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        skipped = events[0]
        assert skipped["hook_name"] == "first-only"
        assert skipped["repo"] == "grip"
        assert skipped["stage"] == "on_enter"
        assert "reason" in skipped

    def test_skipped_no_started_event(self, workspace: Path):
        """Skipped hooks must NOT emit hook.started."""
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="first-only", command="true",
            cwd=str(ctx.repo_root), when="first_materialize", on_failure="block",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert "hook.started" not in types


# ---------------------------------------------------------------------------
# 6. Multiple hooks in sequence
# ---------------------------------------------------------------------------

class TestMultipleHooks:

    def test_two_hooks_both_succeed(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hooks = _make_hooks([
            LifecycleHook(
                stage="on_enter", name="hook-a", command="true",
                cwd=str(ctx.repo_root), when="always", on_failure="block",
            ),
            LifecycleHook(
                stage="on_enter", name="hook-b", command="true",
                cwd=str(ctx.repo_root), when="always", on_failure="block",
            ),
        ])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == [
            "hook.started", "hook.completed",
            "hook.started", "hook.completed",
        ]
        assert events[0]["hook_name"] == "hook-a"
        assert events[2]["hook_name"] == "hook-b"

    def test_second_hook_skipped_first_succeeds(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hooks = _make_hooks([
            LifecycleHook(
                stage="on_enter", name="always-hook", command="true",
                cwd=str(ctx.repo_root), when="always", on_failure="block",
            ),
            LifecycleHook(
                stage="on_enter", name="dirty-only", command="true",
                cwd=str(ctx.repo_root), when="dirty", on_failure="block",
            ),
        ])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        types = [e["type"] for e in events]
        assert types == ["hook.started", "hook.completed", "hook.skipped"]


# ---------------------------------------------------------------------------
# 7. stderr_tail truncation (section 6.3)
# ---------------------------------------------------------------------------

class TestStderrTail:

    def test_stderr_tail_truncated_to_500_bytes(self, workspace: Path):
        ctx = _make_ctx(workspace)
        # Generate > 500 bytes of stderr
        long_stderr_cmd = "python3 -c \"import sys; sys.stderr.write('x' * 1000)\"; false"
        hook = LifecycleHook(
            stage="on_enter", name="noisy", command=long_stderr_cmd,
            cwd=str(ctx.repo_root), when="always", on_failure="warn",
        )
        hooks = _make_hooks([hook])
        run_lifecycle_stage(hooks, "on_enter", ctx, repo_dirty=False, first_materialize=False)
        events = _read_outbox(workspace)
        failed = [e for e in events if e["type"] == "hook.failed"][0]
        assert len(failed["stderr_tail"]) <= 500
