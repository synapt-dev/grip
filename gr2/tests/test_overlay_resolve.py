"""TDD spec: resolver/materialization composition engine.

Tests that resolve.py correctly:
- Detects overlapping files across the active overlay stack
- Selects merge drivers from workspace .gitattributes
- Composes overlapping files through drivers with empty-sentinel fold-start
- Falls back to last-write-wins when no driver pattern matches
- Produces structured errors on composition failure
- Skips resolution for single-overlay stacks

Design contract: config#196 (resolver-materialization-contract-2026-05-15.md).
Contract ratified 2026-05-15. Assertions are concrete.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from gr2_overlay.resolve import (
    _find_overlapping_files,
    _get_driver_for_file,
    resolve_stack,
)

from gr2_overlay.activate import (
    activate_overlay,
    read_active_overlay_stack,
)
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_bare_git_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _init_git_repo(path: Path) -> Path:
    """Initialize a non-bare git repo (needed for git check-attr)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _overlay_meta(overlay_ref: OverlayRef) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author=overlay_ref.author,
        signature="unsigned",
        timestamp="2026-05-15T00:00:00Z",
        parent_overlay_refs=[],
    )


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def _write_gitattributes(workspace_root: Path, content: str) -> None:
    _write_file(workspace_root / ".gitattributes", content)


STANDARD_GITATTRIBUTES = """\
*.toml merge=overlay-deep
*.yml merge=overlay-deep
*.yaml merge=overlay-deep
*.json merge=overlay-deep
COMPOSE.md merge=overlay-prepend
.gitignore merge=overlay-union
"""


def _setup_overlapping_toml_overlays(
    tmp_path: Path,
) -> tuple[Path, Path, OverlayRef, OverlayRef]:
    """Two overlays that both provide agents.toml with disjoint keys.

    Overlay A (alice/base-config): agents.toml with {name = "base"}
    Overlay B (bob/org-config): agents.toml with {org = "synapt"}, theme.toml
    """
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = _init_git_repo(tmp_path / "workspace")

    source_a = tmp_path / "source-a"
    source_a.mkdir()
    _write_file(source_a / "agents.toml", 'name = "base"\n')

    source_b = tmp_path / "source-b"
    source_b.mkdir()
    _write_file(source_b / "agents.toml", 'org = "synapt"\n')
    _write_file(source_b / "theme.toml", 'theme = "dark"\n')

    ref_a = OverlayRef(author="alice", name="base-config")
    ref_b = OverlayRef(author="bob", name="org-config")

    capture_overlay_object(overlay_store, source_a, _overlay_meta(ref_a))
    capture_overlay_object(overlay_store, source_b, _overlay_meta(ref_b))

    write_workspace_allowlist(
        workspace_root,
        [
            {"kind": "path", "pattern": "alice/*", "trust_class": "local"},
            {"kind": "path", "pattern": "bob/*", "trust_class": "local"},
        ],
    )

    return overlay_store, workspace_root, ref_a, ref_b


def _setup_disjoint_overlays(
    tmp_path: Path,
) -> tuple[Path, Path, OverlayRef, OverlayRef]:
    """Two overlays with no shared files (no overlap)."""
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = _init_git_repo(tmp_path / "workspace")

    source_a = tmp_path / "source-a"
    source_a.mkdir()
    _write_file(source_a / "agents.toml", 'name = "base"\n')

    source_b = tmp_path / "source-b"
    source_b.mkdir()
    _write_file(source_b / "theme.toml", 'theme = "dark"\n')

    ref_a = OverlayRef(author="alice", name="base-config")
    ref_b = OverlayRef(author="bob", name="theme-only")

    capture_overlay_object(overlay_store, source_a, _overlay_meta(ref_a))
    capture_overlay_object(overlay_store, source_b, _overlay_meta(ref_b))

    write_workspace_allowlist(
        workspace_root,
        [
            {"kind": "path", "pattern": "alice/*", "trust_class": "local"},
            {"kind": "path", "pattern": "bob/*", "trust_class": "local"},
        ],
    )

    return overlay_store, workspace_root, ref_a, ref_b


def _activate(
    workspace_root: Path,
    overlay_store: Path,
    ref: OverlayRef,
) -> None:
    activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=ref,
        overlay_source_kind="path",
        overlay_source_value=f"{ref.author}/{ref.name}",
        overlay_signer=None,
    )


# ---------------------------------------------------------------------------
# 1. TestOverlapDetection
# ---------------------------------------------------------------------------


class TestOverlapDetection:
    """Contract: _find_overlapping_files returns only files touched by 2+ overlays."""

    def test_single_overlay_file_not_in_overlap_set(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_overlapping_toml_overlays(tmp_path)
        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        stack = read_active_overlay_stack(workspace_root)

        overlaps = _find_overlapping_files(
            workspace_root,
            overlay_store,
            stack,
        )

        assert "theme.toml" not in overlaps

    def test_two_overlays_same_file_detected_as_overlap(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_overlapping_toml_overlays(tmp_path)
        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        stack = read_active_overlay_stack(workspace_root)

        overlaps = _find_overlapping_files(
            workspace_root,
            overlay_store,
            stack,
        )

        assert "agents.toml" in overlaps
        assert ref_a.ref_path in overlaps["agents.toml"]
        assert ref_b.ref_path in overlaps["agents.toml"]

    def test_disjoint_files_no_overlap(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_disjoint_overlays(tmp_path)
        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        stack = read_active_overlay_stack(workspace_root)

        overlaps = _find_overlapping_files(
            workspace_root,
            overlay_store,
            stack,
        )

        assert overlaps == {}

    def test_three_overlays_partial_overlap(self, tmp_path: Path) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "agents.toml", "a = true\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "agents.toml", "b = true\n")
        _write_file(src_b / "theme.toml", "b = true\n")

        src_c = tmp_path / "src-c"
        src_c.mkdir()
        _write_file(src_c / "theme.toml", "c = true\n")

        ref_a = OverlayRef(author="alice", name="a")
        ref_b = OverlayRef(author="bob", name="b")
        ref_c = OverlayRef(author="carol", name="c")

        capture_overlay_object(overlay_store, src_a, _overlay_meta(ref_a))
        capture_overlay_object(overlay_store, src_b, _overlay_meta(ref_b))
        capture_overlay_object(overlay_store, src_c, _overlay_meta(ref_c))

        write_workspace_allowlist(
            workspace_root,
            [
                {"kind": "path", "pattern": "*/*", "trust_class": "local"},
            ],
        )

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        _activate(workspace_root, overlay_store, ref_c)
        stack = read_active_overlay_stack(workspace_root)

        overlaps = _find_overlapping_files(
            workspace_root,
            overlay_store,
            stack,
        )

        assert "agents.toml" in overlaps
        assert len(overlaps["agents.toml"]) == 2
        assert "theme.toml" in overlaps
        assert len(overlaps["theme.toml"]) == 2


# ---------------------------------------------------------------------------
# 2. TestDriverSelection
# ---------------------------------------------------------------------------


class TestDriverSelection:
    """Contract: _get_driver_for_file reads workspace .gitattributes."""

    def test_toml_file_matches_overlay_deep(self, tmp_path: Path) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        driver = _get_driver_for_file(workspace_root, "agents.toml")

        assert driver == "overlay-deep"

    def test_yml_file_matches_overlay_deep(self, tmp_path: Path) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        driver = _get_driver_for_file(workspace_root, "config.yml")

        assert driver == "overlay-deep"

    def test_compose_md_matches_overlay_prepend(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        driver = _get_driver_for_file(workspace_root, "COMPOSE.md")

        assert driver == "overlay-prepend"

    def test_gitignore_matches_overlay_union(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        driver = _get_driver_for_file(workspace_root, ".gitignore")

        assert driver == "overlay-union"

    def test_unknown_driver_raises_error(self, tmp_path: Path) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(
            workspace_root,
            "*.toml merge=custom-driver\n",
        )

        with pytest.raises(ValueError, match="custom-driver"):
            _get_driver_for_file(workspace_root, "agents.toml")

    def test_no_gitattributes_pattern_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        driver = _get_driver_for_file(workspace_root, "README.txt")

        assert driver is None

    def test_no_gitattributes_file_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = _init_git_repo(tmp_path / "workspace")

        driver = _get_driver_for_file(workspace_root, "agents.toml")

        assert driver is None


# ---------------------------------------------------------------------------
# 3. TestDeepMergeComposition
# ---------------------------------------------------------------------------


class TestDeepMergeComposition:
    """Contract: overlay-deep composes TOML via dict-recursive merge.

    Fold starts from EMPTY_SENTINEL. First overlay's content becomes the
    base; subsequent overlays merge on top with overlay-wins-on-conflict.
    """

    def test_two_overlays_disjoint_keys_merged(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_overlapping_toml_overlays(tmp_path)
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)
        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        agents = tomllib.loads(
            (workspace_root / "agents.toml").read_text(),
        )
        assert agents["name"] == "base"
        assert agents["org"] == "synapt"
        assert "agents.toml" in result.resolved_files

    def test_overlay_wins_on_scalar_conflict(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "agents.toml", 'theme = "light"\n')

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "agents.toml", 'theme = "dark"\n')

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        resolve_stack(workspace_root, overlay_store)

        agents = tomllib.loads(
            (workspace_root / "agents.toml").read_text(),
        )
        assert agents["theme"] == "dark"

    def test_nested_dict_merge_preserves_structure(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(
            src_a / "config.toml",
            '[server]\nhost = "localhost"\nport = 8080\n',
        )

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(
            src_b / "config.toml",
            "[server]\nport = 9090\ntimeout = 30\n",
        )

        ref_a = OverlayRef(author="alice", name="base")
        ref_b = OverlayRef(author="bob", name="override")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        resolve_stack(workspace_root, overlay_store)

        config = tomllib.loads(
            (workspace_root / "config.toml").read_text(),
        )
        assert config["server"]["host"] == "localhost"
        assert config["server"]["port"] == 9090
        assert config["server"]["timeout"] == 30

    def test_fold_from_empty_sentinel_yields_first_overlay(
        self,
        tmp_path: Path,
    ) -> None:
        """Single overlapping file with only one overlay should pass through."""
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "agents.toml", 'name = "base"\nrole = "agent"\n')

        ref_a = OverlayRef(author="alice", name="solo")
        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)

        result = resolve_stack(workspace_root, overlay_store)

        agents = tomllib.loads(
            (workspace_root / "agents.toml").read_text(),
        )
        assert agents["name"] == "base"
        assert agents["role"] == "agent"
        assert result.resolved_files == []
        assert result.errors == []

    def test_three_overlay_fold_order(self, tmp_path: Path) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        for label, content in [
            ("a", 'key_a = "a"\nshared = "a"\n'),
            ("b", 'key_b = "b"\nshared = "b"\n'),
            ("c", 'key_c = "c"\nshared = "c"\n'),
        ]:
            src = tmp_path / f"src-{label}"
            src.mkdir()
            _write_file(src / "agents.toml", content)

        ref_a = OverlayRef(author="alice", name="a")
        ref_b = OverlayRef(author="bob", name="b")
        ref_c = OverlayRef(author="carol", name="c")

        for src_name, ref in [
            ("src-a", ref_a),
            ("src-b", ref_b),
            ("src-c", ref_c),
        ]:
            capture_overlay_object(
                overlay_store,
                tmp_path / src_name,
                _overlay_meta(ref),
            )

        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        _activate(workspace_root, overlay_store, ref_c)

        resolve_stack(workspace_root, overlay_store)

        agents = tomllib.loads(
            (workspace_root / "agents.toml").read_text(),
        )
        assert agents["key_a"] == "a"
        assert agents["key_b"] == "b"
        assert agents["key_c"] == "c"
        assert agents["shared"] == "c"


# ---------------------------------------------------------------------------
# 4. TestPrependComposition
# ---------------------------------------------------------------------------


class TestPrependComposition:
    """Contract: overlay-prepend concatenates in stack order.

    Driver semantics: current = other + current (prepend other onto current).
    Fold from empty: first overlay becomes current, subsequent prepend on top.
    Result: last-activated overlay's content appears first.
    """

    def test_two_overlays_compose_md_concatenation(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "COMPOSE.md", "# Base Config\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "COMPOSE.md", "# Org Override\n")

        ref_a = OverlayRef(author="alice", name="base")
        ref_b = OverlayRef(author="bob", name="org")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        content = (workspace_root / "COMPOSE.md").read_text()
        assert content == "# Org Override\n# Base Config\n"
        assert "COMPOSE.md" in result.resolved_files

    def test_stack_order_determines_prepend_order(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "COMPOSE.md", "AAA\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "COMPOSE.md", "BBB\n")

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_b)
        _activate(workspace_root, overlay_store, ref_a)

        resolve_stack(workspace_root, overlay_store)

        content = (workspace_root / "COMPOSE.md").read_text()
        assert content == "AAA\nBBB\n"


# ---------------------------------------------------------------------------
# 5. TestUnionComposition
# ---------------------------------------------------------------------------


class TestUnionComposition:
    """Contract: overlay-union deduplicates lines across overlays.

    Driver semantics: keep current lines, append unique lines from other.
    Fold from empty: first overlay's lines become current, then subsequent
    overlays' unique lines are appended.
    """

    def test_two_overlays_gitignore_deduplicated(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / ".gitignore", "*.pyc\n.env\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / ".gitignore", "*.pyc\n__pycache__/\n")

        ref_a = OverlayRef(author="alice", name="base")
        ref_b = OverlayRef(author="bob", name="extra")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        lines = (workspace_root / ".gitignore").read_text().splitlines()
        assert lines.count("*.pyc") == 1
        assert ".env" in lines
        assert "__pycache__/" in lines
        assert ".gitignore" in result.resolved_files

    def test_unique_lines_from_both_overlays_preserved(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / ".gitignore", "only-in-a\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / ".gitignore", "only-in-b\n")

        ref_a = OverlayRef(author="alice", name="base")
        ref_b = OverlayRef(author="bob", name="extra")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        resolve_stack(workspace_root, overlay_store)

        lines = (workspace_root / ".gitignore").read_text().splitlines()
        assert "only-in-a" in lines
        assert "only-in-b" in lines


# ---------------------------------------------------------------------------
# 6. TestLastWriteWinsFallback
# ---------------------------------------------------------------------------


class TestLastWriteWinsFallback:
    """Contract: files with no driver pattern use last-write-wins."""

    def test_no_driver_pattern_highest_priority_wins(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "README.txt", "Version A\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "README.txt", "Version B\n")

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        content = (workspace_root / "README.txt").read_text()
        assert content == "Version B\n"
        assert "README.txt" in result.passthrough_files

    def test_passthrough_file_listed_in_result(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "data.bin", "binary-a")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "data.bin", "binary-b")

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        assert "data.bin" in result.passthrough_files
        assert "data.bin" not in result.resolved_files


# ---------------------------------------------------------------------------
# 7. TestCompositionError
# ---------------------------------------------------------------------------


class TestCompositionError:
    """Contract: driver failures produce structured errors, not corruption.

    Errors are collected in ResolveResult.errors. Files that composed
    successfully remain composed. Failing files retain last-write-wins.
    """

    def test_driver_exception_produces_structured_error(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "bad.toml", 'valid = "toml"\n')

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "bad.toml", "{{{not valid toml}}}")

        ref_a = OverlayRef(author="alice", name="good")
        ref_b = OverlayRef(author="bob", name="bad")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, "*.toml merge=overlay-deep\n")

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        assert len(result.errors) == 1
        assert result.errors[0].file_path == "bad.toml"
        assert result.errors[0].driver == "overlay-deep"

    def test_partial_resolution_leaves_successful_files_composed(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "good.toml", "a = 1\n")
        _write_file(src_a / "bad.toml", "valid = true\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "good.toml", "b = 2\n")
        _write_file(src_b / "bad.toml", "{{{invalid}}}")

        ref_a = OverlayRef(author="alice", name="base")
        ref_b = OverlayRef(author="bob", name="broken")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, "*.toml merge=overlay-deep\n")

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        good = tomllib.loads(
            (workspace_root / "good.toml").read_text(),
        )
        assert good["a"] == 1
        assert good["b"] == 2
        assert "good.toml" in result.resolved_files
        assert len(result.errors) == 1

    def test_no_conflict_markers_in_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "agents.toml", 'theme = "light"\n')

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "agents.toml", 'theme = "dark"\n')

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        resolve_stack(workspace_root, overlay_store)

        for path in workspace_root.rglob("*"):
            if path.is_file() and not str(path).startswith(
                str(workspace_root / ".grip"),
            ):
                content = path.read_text()
                assert "<<<<<<<" not in content, f"Conflict markers in {path}"


# ---------------------------------------------------------------------------
# 8. TestSingleOverlaySkipsResolution
# ---------------------------------------------------------------------------


class TestSingleOverlaySkipsResolution:
    """Contract: single overlay on stack = no resolution needed."""

    def test_single_overlay_returns_empty_resolve_result(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src = tmp_path / "src"
        src.mkdir()
        _write_file(src / "agents.toml", 'name = "solo"\n')

        ref = OverlayRef(author="alice", name="only")
        capture_overlay_object(overlay_store, src, _overlay_meta(ref))
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )

        _activate(workspace_root, overlay_store, ref)

        result = resolve_stack(workspace_root, overlay_store)

        assert result.resolved_files == []
        assert result.passthrough_files == []
        assert result.errors == []

    def test_single_overlay_files_unchanged_on_disk(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src = tmp_path / "src"
        src.mkdir()
        _write_file(src / "agents.toml", 'name = "solo"\n')

        ref = OverlayRef(author="alice", name="only")
        capture_overlay_object(overlay_store, src, _overlay_meta(ref))
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )

        _activate(workspace_root, overlay_store, ref)
        content_before = (workspace_root / "agents.toml").read_text()

        resolve_stack(workspace_root, overlay_store)

        content_after = (workspace_root / "agents.toml").read_text()
        assert content_after == content_before


# ---------------------------------------------------------------------------
# 9. TestResolveResult
# ---------------------------------------------------------------------------


class TestResolveResult:
    """Contract: ResolveResult correctly categorizes files."""

    def test_resolved_files_are_those_composed_through_drivers(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_overlapping_toml_overlays(tmp_path)
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)
        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        assert "agents.toml" in result.resolved_files
        assert "theme.toml" not in result.resolved_files

    def test_passthrough_files_are_overlapping_without_driver(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "notes.txt", "from A\n")
        _write_file(src_a / "agents.toml", "a = true\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "notes.txt", "from B\n")
        _write_file(src_b / "agents.toml", "b = true\n")

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, STANDARD_GITATTRIBUTES)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        assert "notes.txt" in result.passthrough_files
        assert "agents.toml" in result.resolved_files

    def test_errors_contain_failed_compositions(
        self,
        tmp_path: Path,
    ) -> None:
        overlay_store = _init_bare_git_repo(
            tmp_path / "overlay-store.git",
        )
        workspace_root = _init_git_repo(tmp_path / "workspace")

        src_a = tmp_path / "src-a"
        src_a.mkdir()
        _write_file(src_a / "broken.toml", "ok = true\n")

        src_b = tmp_path / "src-b"
        src_b.mkdir()
        _write_file(src_b / "broken.toml", "NOT_TOML: [[[")

        ref_a = OverlayRef(author="alice", name="first")
        ref_b = OverlayRef(author="bob", name="second")

        capture_overlay_object(
            overlay_store,
            src_a,
            _overlay_meta(ref_a),
        )
        capture_overlay_object(
            overlay_store,
            src_b,
            _overlay_meta(ref_b),
        )
        write_workspace_allowlist(
            workspace_root,
            [{"kind": "path", "pattern": "*/*", "trust_class": "local"}],
        )
        _write_gitattributes(workspace_root, "*.toml merge=overlay-deep\n")

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        result = resolve_stack(workspace_root, overlay_store)

        assert len(result.errors) >= 1
        error_files = [e.file_path for e in result.errors]
        assert "broken.toml" in error_files
