"""TDD spec: file projections never modify a git-TRACKED destination.

Reproduction that motivated this spec: a repo's `.gr2/hooks.toml` carried
a projection linking a workspace-level instructions file over
`{repo_root}/CLAUDE.md` with `if_exists = "overwrite"`, and the same repo
also tracks its own `CLAUDE.md` (a 100644 file). Every materialization of
that repo therefore replaced tracked content with a symlink into another
repo's checkout: the clone came out type-dirty (`T CLAUDE.md`), which
blocks freezes and any tree-exactness assertion, and the repo's own doc
was shadowed.

The invariant this pins: **a file projection writes untracked space
only.** A tracked destination is the repo's own content; overwriting it
is never a projection's job, whatever `if_exists` says. The skip must be
a named, auditable result — silent success and silent absence are both
wrong (the receipt is how a caller learns the projection did not apply).

The design question this does NOT decide (deliberately left to the
instructions file's owner): how such a repo should receive workspace-
level instructions — an untracked carrier name, or a reruled hooks.toml.
This guard only stops the tree mutation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli.hooks import (
    FileProjection,
    apply_file_projections,
)

from tests.test_hook_hardening import _make_ctx, _make_hooks

import subprocess


def _track(repo_root: Path, *relpaths: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "add", *relpaths], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-q", "-m", "init",
         "--", *relpaths],
        check=True,
    )


class TestProjectionNeverModifiesTrackedDest:
    """A projection onto a tracked path is a named skip, not a write."""

    def test_link_onto_tracked_file_is_skipped_and_tree_stays_clean(
        self, workspace: Path
    ):
        ctx = _make_ctx(workspace)
        dest = ctx.repo_root / "CLAUDE.md"
        tracked_bytes = b"repo's own dev guide\n"
        dest.write_bytes(tracked_bytes)
        _track(ctx.repo_root, "CLAUDE.md")

        src = workspace / "config" / "claude.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"gripspace instructions\n")

        hooks = _make_hooks(
            links=[
                FileProjection(
                    kind="link",
                    src=str(src),
                    dest=str(dest),
                    if_exists="overwrite",
                )
            ]
        )
        results = apply_file_projections(hooks, ctx)

        assert len(results) == 1
        assert results[0].status == "skipped"
        assert "tracked" in results[0].detail
        # The tree is untouched: bytes intact, no symlink, git clean.
        assert dest.is_symlink() is False
        assert dest.read_bytes() == tracked_bytes

    def test_skip_applies_to_every_if_exists_policy(
        self, workspace: Path
    ):
        """overwrite / skip / error all refuse the same way: tracked is not
        a policy question, it is out of the projection's jurisdiction."""
        ctx = _make_ctx(workspace)
        dest = ctx.repo_root / "CLAUDE.md"
        dest.write_bytes(b"tracked\n")
        _track(ctx.repo_root, "CLAUDE.md")
        src = workspace / "config" / "claude.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"other\n")

        for policy in ("overwrite", "skip", "error", "merge"):
            hooks = _make_hooks(
                links=[
                    FileProjection(
                        kind="link",
                        src=str(src),
                        dest=str(dest),
                        if_exists=policy,
                    )
                ]
            )
            results = apply_file_projections(hooks, ctx)
            assert results[0].status == "skipped", policy
            assert "tracked" in results[0].detail, policy
            assert not dest.is_symlink(), policy

    def test_projection_onto_untracked_dest_still_honors_policy(
        self, workspace: Path
    ):
        """Control: the guard must not over-block. An untracked existing
        dest with if_exists=overwrite still applies."""
        ctx = _make_ctx(workspace)
        dest = ctx.repo_root / "LOCAL.md"  # exists, untracked
        dest.write_bytes(b"local\n")
        (ctx.repo_root / "README.md").write_bytes(b"tracked other\n")
        _track(ctx.repo_root, "README.md")  # repo exists, other file tracked
        src = workspace / "config" / "claude.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"gripspace\n")

        hooks = _make_hooks(
            links=[
                FileProjection(
                    kind="link",
                    src=str(src),
                    dest=str(dest),
                    if_exists="overwrite",
                )
            ]
        )
        results = apply_file_projections(hooks, ctx)

        assert results[0].status == "applied"
        assert dest.is_symlink()

    def test_copy_projection_onto_tracked_file_is_skipped_too(
        self, workspace: Path
    ):
        """The invariant is about the DESTINATION, not the mechanism: a
        copy projection onto a tracked path skips the same way."""
        ctx = _make_ctx(workspace)
        dest = ctx.repo_root / "CLAUDE.md"
        dest.write_bytes(b"tracked\n")
        _track(ctx.repo_root, "CLAUDE.md")
        src = workspace / "config" / "claude.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"other\n")

        hooks = _make_hooks(
            copies=[
                FileProjection(
                    kind="copy",
                    src=str(src),
                    dest=str(dest),
                    if_exists="overwrite",
                )
            ]
        )
        results = apply_file_projections(hooks, ctx)

        assert results[0].status == "skipped"
        assert "tracked" in results[0].detail
        assert dest.read_bytes() == b"tracked\n"