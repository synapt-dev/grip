"""TDD specs for grip#564: harden Python gr2 hook runtime semantics.

Tests enforce three HOOK-CONFIG-MODEL.md requirements that the current
runtime does not fully implement:

1. Template variable validation (section 3.5):
   "undefined variables are validation errors"
2. {unit_root} template variable support (section 3.5):
   listed as an allowed variable but missing from HookContext
3. File projection result metadata (section 3.2):
   results must include the if_exists policy for auditability

Also covers edge-case combinations of when/on_failure that the
existing test_hook_events.py does not exercise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli.hooks import (
    FileProjection,
    HookContext,
    HookResult,
    HookRuntimeError,
    LifecycleHook,
    RepoHooks,
    apply_file_projections,
    render_path,
    render_text,
    run_lifecycle_stage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(workspace: Path, *, unit_path: str = "agents/apollo") -> HookContext:
    repo_root = workspace / "repos" / "grip"
    repo_root.mkdir(parents=True, exist_ok=True)
    return HookContext(
        workspace_root=workspace,
        unit_root=workspace / unit_path,
        lane_root=workspace / "lanes" / "apollo" / "feat-test",
        repo_root=repo_root,
        repo_name="grip",
        lane_owner="apollo",
        lane_subject="grip",
        lane_name="feat/test",
    )


def _make_hooks(
    *,
    links: list[FileProjection] | None = None,
    copies: list[FileProjection] | None = None,
    lifecycle: list[LifecycleHook] | None = None,
    stage: str = "on_enter",
) -> RepoHooks:
    kwargs = {"on_materialize": [], "on_enter": [], "on_exit": []}
    if lifecycle:
        kwargs[stage] = lifecycle
    return RepoHooks(
        repo_name="grip",
        file_links=links or [],
        file_copies=copies or [],
        policy={},
        path=Path("/fake/.gr2/hooks.toml"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Template variable validation (HOOK-CONFIG-MODEL.md section 3.5)
# ---------------------------------------------------------------------------

class TestTemplateValidation:
    """Undefined template variables must be validation errors."""

    def test_valid_variables_render_cleanly(self, workspace: Path):
        ctx = _make_ctx(workspace)
        result = render_text(
            "{workspace_root}/repos/{repo_name}/src",
            ctx,
        )
        assert "{" not in result
        assert str(workspace) in result
        assert "grip" in result

    def test_unit_root_renders(self, workspace: Path):
        """{unit_root} is listed in the spec and must be supported."""
        ctx = _make_ctx(workspace, unit_path="agents/apollo")
        result = render_text("{unit_root}/repos/synapt", ctx)
        assert str(workspace / "agents" / "apollo") in result
        assert "{" not in result

    def test_all_spec_variables_render(self, workspace: Path):
        """Every variable from HOOK-CONFIG-MODEL.md section 3.5 must render."""
        ctx = _make_ctx(workspace)
        template = (
            "{workspace_root} {unit_root} {lane_root} "
            "{repo_root} {repo_name} {lane_owner} "
            "{lane_subject} {lane_name}"
        )
        result = render_text(template, ctx)
        assert "{" not in result, f"Unresolved variables in: {result}"

    def test_undefined_variable_raises(self, workspace: Path):
        """A template with {unknown_var} must raise, not pass through."""
        ctx = _make_ctx(workspace)
        with pytest.raises((ValueError, SystemExit)):
            render_text("{workspace_root}/{undefined_var}/file", ctx)

    def test_partial_undefined_raises(self, workspace: Path):
        """Mix of valid and invalid variables still raises."""
        ctx = _make_ctx(workspace)
        with pytest.raises((ValueError, SystemExit)):
            render_text("{repo_root}/{bogus}", ctx)

    def test_render_path_validates(self, workspace: Path):
        """render_path must also validate template variables."""
        ctx = _make_ctx(workspace)
        with pytest.raises((ValueError, SystemExit)):
            render_path("{not_a_var}/subdir", ctx)


# ---------------------------------------------------------------------------
# 2. File projection result metadata (HOOK-CONFIG-MODEL.md section 3.2)
# ---------------------------------------------------------------------------

class TestProjectionResultMetadata:
    """Projection results must include the if_exists policy for audit."""

    def test_applied_result_includes_if_exists(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src_file = ctx.repo_root / "README.md"
        src_file.write_text("hello")
        proj = FileProjection(
            kind="copy",
            src="README.md",
            dest="{workspace_root}/projected-readme.md",
            if_exists="error",
        )
        hooks = _make_hooks(copies=[proj])
        results = apply_file_projections(hooks, ctx)
        assert len(results) == 1
        r = results[0]
        assert r.status == "applied"
        assert r.if_exists == "error"

    def test_skipped_result_includes_if_exists(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src_file = ctx.repo_root / "README.md"
        src_file.write_text("hello")
        dest = workspace / "projected-readme.md"
        dest.write_text("existing")
        proj = FileProjection(
            kind="copy",
            src="README.md",
            dest="{workspace_root}/projected-readme.md",
            if_exists="skip",
        )
        hooks = _make_hooks(copies=[proj])
        results = apply_file_projections(hooks, ctx)
        assert len(results) == 1
        r = results[0]
        assert r.status == "skipped"
        assert r.if_exists == "skip"

    def test_overwrite_result_includes_if_exists(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src_file = ctx.repo_root / "README.md"
        src_file.write_text("new content")
        dest = workspace / "projected-readme.md"
        dest.write_text("old content")
        proj = FileProjection(
            kind="copy",
            src="README.md",
            dest="{workspace_root}/projected-readme.md",
            if_exists="overwrite",
        )
        hooks = _make_hooks(copies=[proj])
        results = apply_file_projections(hooks, ctx)
        assert len(results) == 1
        r = results[0]
        assert r.status == "applied"
        assert r.if_exists == "overwrite"
        assert dest.read_text() == "new content"


# ---------------------------------------------------------------------------
# 3. Lifecycle when/on_failure edge cases
# ---------------------------------------------------------------------------

class TestLifecycleEdgeCases:

    def test_dirty_hook_runs_when_repo_dirty(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_exit", name="save-state", command="true",
            cwd=str(ctx.repo_root), when="dirty", on_failure="warn",
        )
        hooks = _make_hooks(lifecycle=[hook], stage="on_exit")
        results = run_lifecycle_stage(
            hooks, "on_exit", ctx,
            repo_dirty=True, first_materialize=False,
        )
        assert results[0].status == "applied"

    def test_dirty_hook_skipped_when_clean(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_exit", name="save-state", command="true",
            cwd=str(ctx.repo_root), when="dirty", on_failure="warn",
        )
        hooks = _make_hooks(lifecycle=[hook], stage="on_exit")
        results = run_lifecycle_stage(
            hooks, "on_exit", ctx,
            repo_dirty=False, first_materialize=False,
        )
        assert results[0].status == "skipped"

    def test_manual_hook_skipped_by_default(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="reindex", command="true",
            cwd=str(ctx.repo_root), when="manual", on_failure="block",
        )
        hooks = _make_hooks(lifecycle=[hook])
        results = run_lifecycle_stage(
            hooks, "on_enter", ctx,
            repo_dirty=False, first_materialize=False,
        )
        assert results[0].status == "skipped"

    def test_manual_hook_runs_with_allow_manual(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_enter", name="reindex", command="true",
            cwd=str(ctx.repo_root), when="manual", on_failure="block",
        )
        hooks = _make_hooks(lifecycle=[hook])
        results = run_lifecycle_stage(
            hooks, "on_enter", ctx,
            repo_dirty=False, first_materialize=False,
            allow_manual=True,
        )
        assert results[0].status == "applied"

    def test_first_materialize_skipped_on_reenter(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_materialize", name="setup", command="true",
            cwd=str(ctx.repo_root), when="first_materialize", on_failure="block",
        )
        hooks = _make_hooks(lifecycle=[hook], stage="on_materialize")
        results = run_lifecycle_stage(
            hooks, "on_materialize", ctx,
            repo_dirty=False, first_materialize=False,
        )
        assert results[0].status == "skipped"

    def test_first_materialize_runs_on_first(self, workspace: Path):
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_materialize", name="setup", command="true",
            cwd=str(ctx.repo_root), when="first_materialize", on_failure="block",
        )
        hooks = _make_hooks(lifecycle=[hook], stage="on_materialize")
        results = run_lifecycle_stage(
            hooks, "on_materialize", ctx,
            repo_dirty=False, first_materialize=True,
        )
        assert results[0].status == "applied"

    def test_on_materialize_block_stops_execution(self, workspace: Path):
        """on_materialize hooks default to on_failure=block per spec."""
        ctx = _make_ctx(workspace)
        hook = LifecycleHook(
            stage="on_materialize", name="install", command="false",
            cwd=str(ctx.repo_root), when="always", on_failure="block",
        )
        hooks = _make_hooks(lifecycle=[hook], stage="on_materialize")
        with pytest.raises(HookRuntimeError):
            run_lifecycle_stage(
                hooks, "on_materialize", ctx,
                repo_dirty=False, first_materialize=True,
            )

    def test_on_exit_warn_continues(self, workspace: Path):
        """on_exit hooks default to on_failure=warn per spec."""
        ctx = _make_ctx(workspace)
        hooks_list = [
            LifecycleHook(
                stage="on_exit", name="cleanup", command="false",
                cwd=str(ctx.repo_root), when="always", on_failure="warn",
            ),
            LifecycleHook(
                stage="on_exit", name="report", command="true",
                cwd=str(ctx.repo_root), when="always", on_failure="warn",
            ),
        ]
        hooks = _make_hooks(lifecycle=hooks_list, stage="on_exit")
        results = run_lifecycle_stage(
            hooks, "on_exit", ctx,
            repo_dirty=False, first_materialize=False,
        )
        assert len(results) == 2
        assert results[0].status == "warned"
        assert results[1].status == "applied"


# ---------------------------------------------------------------------------
# 4. File projection conflict handling
# ---------------------------------------------------------------------------

class TestProjectionConflicts:

    def test_error_on_conflict(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src = ctx.repo_root / "CLAUDE.md"
        src.write_text("instructions")
        dest = workspace / "CLAUDE.md"
        dest.write_text("existing")
        proj = FileProjection(
            kind="copy", src="CLAUDE.md",
            dest="{workspace_root}/CLAUDE.md", if_exists="error",
        )
        hooks = _make_hooks(copies=[proj])
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)

    def test_link_projection_creates_symlink(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src = ctx.repo_root / "CLAUDE.md"
        src.write_text("instructions")
        proj = FileProjection(
            kind="link", src="CLAUDE.md",
            dest="{workspace_root}/CLAUDE-link.md", if_exists="error",
        )
        hooks = _make_hooks(links=[proj])
        results = apply_file_projections(hooks, ctx)
        dest = workspace / "CLAUDE-link.md"
        assert dest.is_symlink()
        assert dest.read_text() == "instructions"
        assert results[0].status == "applied"

    def test_missing_source_blocks(self, workspace: Path):
        ctx = _make_ctx(workspace)
        proj = FileProjection(
            kind="copy", src="nonexistent.md",
            dest="{workspace_root}/out.md", if_exists="error",
        )
        hooks = _make_hooks(copies=[proj])
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)

    def test_merge_not_implemented(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src = ctx.repo_root / "config.toml"
        src.write_text("[settings]")
        dest = workspace / "config.toml"
        dest.write_text("[existing]")
        proj = FileProjection(
            kind="copy", src="config.toml",
            dest="{workspace_root}/config.toml", if_exists="merge",
        )
        hooks = _make_hooks(copies=[proj])
        with pytest.raises(HookRuntimeError, match="merge"):
            apply_file_projections(hooks, ctx)

    def test_overwrite_replaces_existing_file(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src = ctx.repo_root / "data.json"
        src.write_text('{"new": true}')
        dest = workspace / "data.json"
        dest.write_text('{"old": true}')
        proj = FileProjection(
            kind="copy", src="data.json",
            dest="{workspace_root}/data.json", if_exists="overwrite",
        )
        hooks = _make_hooks(copies=[proj])
        results = apply_file_projections(hooks, ctx)
        assert dest.read_text() == '{"new": true}'
        assert results[0].status == "applied"

    def test_overwrite_refuses_directory(self, workspace: Path):
        ctx = _make_ctx(workspace)
        src = ctx.repo_root / "data.json"
        src.write_text("{}")
        dest = workspace / "data.json"
        dest.mkdir(parents=True, exist_ok=True)
        proj = FileProjection(
            kind="copy", src="data.json",
            dest="{workspace_root}/data.json", if_exists="overwrite",
        )
        hooks = _make_hooks(copies=[proj])
        with pytest.raises(HookRuntimeError, match="directory"):
            apply_file_projections(hooks, ctx)

    def test_unit_root_in_projection_dest(self, workspace: Path):
        """{unit_root} must work in file projection destinations."""
        ctx = _make_ctx(workspace, unit_path="agents/apollo")
        unit_root = workspace / "agents" / "apollo"
        unit_root.mkdir(parents=True, exist_ok=True)
        src = ctx.repo_root / ".env.example"
        src.write_text("DB_URL=localhost")
        proj = FileProjection(
            kind="copy", src=".env.example",
            dest="{unit_root}/.env.example", if_exists="error",
        )
        hooks = _make_hooks(copies=[proj])
        results = apply_file_projections(hooks, ctx)
        assert (unit_root / ".env.example").read_text() == "DB_URL=localhost"
        assert results[0].status == "applied"
