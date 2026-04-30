from __future__ import annotations

from pathlib import Path

import pytest


def test_apply_unit_applies_dependency_chain_in_topological_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units

    workspace_root = tmp_path / "workspace"
    _write_manifest(workspace_root, "base-theme", depends_on=[])
    _write_manifest(workspace_root, "feature-auth", depends_on=["base-theme"])
    _write_manifest(workspace_root, "landing-page", depends_on=["feature-auth"])

    observed: list[str] = []

    def fake_apply_single_unit(*, workspace_root: Path, unit_name: str):
        observed.append(unit_name)
        return {"unit_name": unit_name, "status": "ok"}

    monkeypatch.setattr(units, "_apply_single_unit", fake_apply_single_unit)

    result = units.apply_unit(workspace_root=workspace_root, unit_name="landing-page")

    assert observed == ["base-theme", "feature-auth", "landing-page"]
    assert result["applied_units"] == ["base-theme", "feature-auth", "landing-page"]
    assert result["status"] == "ok"


def test_apply_unit_rejects_cycle_in_depends_on_chain(tmp_path: Path) -> None:
    from gr2_overlay.units import apply_unit

    workspace_root = tmp_path / "workspace"
    _write_manifest(workspace_root, "base-theme", depends_on=["landing-page"])
    _write_manifest(workspace_root, "feature-auth", depends_on=["base-theme"])
    _write_manifest(workspace_root, "landing-page", depends_on=["feature-auth"])

    with pytest.raises(ValueError, match="dependency cycle"):
        apply_unit(workspace_root=workspace_root, unit_name="landing-page")


def test_apply_unit_rejects_missing_dependency_manifest(tmp_path: Path) -> None:
    from gr2_overlay.units import apply_unit

    workspace_root = tmp_path / "workspace"
    _write_manifest(workspace_root, "feature-auth", depends_on=["base-theme"])

    with pytest.raises(FileNotFoundError, match="base-theme"):
        apply_unit(workspace_root=workspace_root, unit_name="feature-auth")


def test_apply_unit_stops_downstream_apply_when_dependency_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units

    workspace_root = tmp_path / "workspace"
    _write_manifest(workspace_root, "base-theme", depends_on=[])
    _write_manifest(workspace_root, "feature-auth", depends_on=["base-theme"])
    _write_manifest(workspace_root, "landing-page", depends_on=["feature-auth"])

    observed: list[str] = []

    def fake_apply_single_unit(*, workspace_root: Path, unit_name: str):
        observed.append(unit_name)
        if unit_name == "feature-auth":
            raise RuntimeError("feature-auth failed")
        return {"unit_name": unit_name, "status": "ok"}

    monkeypatch.setattr(units, "_apply_single_unit", fake_apply_single_unit)

    with pytest.raises(RuntimeError, match="feature-auth failed"):
        units.apply_unit(workspace_root=workspace_root, unit_name="landing-page")

    assert observed == ["base-theme", "feature-auth"]


def _write_manifest(workspace_root: Path, unit_name: str, depends_on: list[str]) -> None:
    path = workspace_root / ".grip" / "units" / f"{unit_name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    depends_literal = "[" + ", ".join(f'"{name}"' for name in depends_on) + "]"
    path.write_text(
        (
            "version = 1\n"
            'scope = "workspace"\n'
            'target_base_ref = "refs/heads/main"\n'
            f"depends_on = {depends_literal}\n"
            'on_failure = "rollback"\n\n'
            "[[source_overlays]]\n"
            'repo_name = "app"\n'
            f'overlay_ref = "refs/overlays/team/{unit_name}"\n'
            'overlay_source_kind = "path"\n'
            f'overlay_source_value = "team/{unit_name}"\n'
        )
    )
