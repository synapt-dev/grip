from __future__ import annotations

from pathlib import Path

import pytest

from gr2_overlay.workspace_spec import (
    OverlaySpecEntry,
    load_overlay_spec,
    overlay_spec_path,
    validate_overlay_spec,
)


def test_overlay_spec_lives_in_grip_directory(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    assert overlay_spec_path(workspace_root) == workspace_root / ".grip" / "overlays.toml"


def test_load_parses_overlay_entries_with_all_fields(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    spec_path = workspace_root / ".grip" / "overlays.toml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        """
[[overlays]]
name = "theme-dark"
path = "overlays/theme-dark"
applies_to = ["synapt", "gitgrip"]
priority = 10

[[overlays]]
name = "shared-base"
path = "overlays/shared-base"
applies_to = ["*"]
priority = 0
"""
    )

    entries = load_overlay_spec(workspace_root)
    assert entries == [
        OverlaySpecEntry(
            name="theme-dark",
            path="overlays/theme-dark",
            applies_to=["synapt", "gitgrip"],
            priority=10,
        ),
        OverlaySpecEntry(
            name="shared-base",
            path="overlays/shared-base",
            applies_to=["*"],
            priority=0,
        ),
    ]


def test_load_returns_empty_list_when_no_spec_file_exists(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    assert load_overlay_spec(workspace_root) == []


def test_load_returns_empty_list_for_empty_spec_file(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    spec_path = workspace_root / ".grip" / "overlays.toml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("")

    assert load_overlay_spec(workspace_root) == []


def test_priority_defaults_to_zero_when_omitted(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    spec_path = workspace_root / ".grip" / "overlays.toml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        """
[[overlays]]
name = "minimal"
path = "overlays/minimal"
applies_to = ["*"]
"""
    )

    entries = load_overlay_spec(workspace_root)
    assert len(entries) == 1
    assert entries[0].priority == 0


def test_validate_rejects_duplicate_overlay_names(tmp_path: Path) -> None:
    entries = [
        OverlaySpecEntry(name="dupe", path="a", applies_to=["*"], priority=0),
        OverlaySpecEntry(name="dupe", path="b", applies_to=["*"], priority=1),
    ]

    with pytest.raises(ValueError, match="Duplicate overlay name"):
        validate_overlay_spec(entries)


def test_validate_rejects_empty_name(tmp_path: Path) -> None:
    entries = [
        OverlaySpecEntry(name="", path="overlays/x", applies_to=["*"], priority=0),
    ]

    with pytest.raises(ValueError, match="name"):
        validate_overlay_spec(entries)


def test_validate_rejects_empty_path(tmp_path: Path) -> None:
    entries = [
        OverlaySpecEntry(name="valid", path="", applies_to=["*"], priority=0),
    ]

    with pytest.raises(ValueError, match="path"):
        validate_overlay_spec(entries)


def test_validate_rejects_empty_applies_to(tmp_path: Path) -> None:
    entries = [
        OverlaySpecEntry(name="valid", path="overlays/x", applies_to=[], priority=0),
    ]

    with pytest.raises(ValueError, match="applies_to"):
        validate_overlay_spec(entries)


def test_validate_rejects_negative_priority(tmp_path: Path) -> None:
    entries = [
        OverlaySpecEntry(name="valid", path="overlays/x", applies_to=["*"], priority=-1),
    ]

    with pytest.raises(ValueError, match="priority"):
        validate_overlay_spec(entries)


def test_validate_passes_for_well_formed_spec() -> None:
    entries = [
        OverlaySpecEntry(name="base", path="overlays/base", applies_to=["*"], priority=0),
        OverlaySpecEntry(name="theme", path="overlays/theme", applies_to=["synapt"], priority=10),
    ]

    validate_overlay_spec(entries)


def test_entries_sort_by_priority_ascending() -> None:
    entries = [
        OverlaySpecEntry(name="high", path="a", applies_to=["*"], priority=20),
        OverlaySpecEntry(name="low", path="b", applies_to=["*"], priority=5),
        OverlaySpecEntry(name="mid", path="c", applies_to=["*"], priority=10),
    ]

    sorted_entries = sorted(entries, key=lambda e: e.priority)
    assert [e.name for e in sorted_entries] == ["low", "mid", "high"]
