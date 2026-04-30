"""M2.3 validation: hindsight reference overlay end-to-end.

Exercises the M1 substrate against real overlay content from
config/overlays/hindsight/. Proves capture -> activate -> verify
for the third reference repo overlay (SOTA on LOCOMO + LongMemEval).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.activate import (
    activate_overlay,
    deactivate_overlay,
    read_active_overlay_stack,
)
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.refs import fetch_overlay_ref, push_overlay_ref
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel

OVERLAY_SOURCE = Path(__file__).resolve().parent.parent.parent / "config" / "overlays" / "hindsight"

EXPECTED_FILES = {
    "COMPOSE.md",
    "settings.toml",
    "analysis.yml",
}


def test_hindsight_overlay_source_exists_and_has_expected_files() -> None:
    assert OVERLAY_SOURCE.is_dir(), f"Overlay source missing: {OVERLAY_SOURCE}"
    actual = {p.name for p in OVERLAY_SOURCE.iterdir() if p.is_file()}
    assert EXPECTED_FILES == actual, f"Expected {EXPECTED_FILES}, got {actual}"


def test_hindsight_overlay_capture_and_activate_roundtrip(tmp_path: Path) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local.git")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    overlay_ref = OverlayRef(author="synapt", name="hindsight")

    capture_overlay_object(
        local_store,
        OVERLAY_SOURCE,
        _overlay_meta(overlay_ref),
    )

    result = activate_overlay(
        workspace_root=workspace,
        overlay_store=local_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="synapt/hindsight",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert result.completed == ["overlay.activated"]

    for filename in EXPECTED_FILES:
        target = workspace / filename
        source = OVERLAY_SOURCE / filename
        assert target.exists(), f"File not materialized: {filename}"
        assert target.read_text() == source.read_text(), f"Content mismatch: {filename}"


def test_hindsight_overlay_content_fidelity_across_push_fetch(tmp_path: Path) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote.git")
    peer_store = _init_bare_git_repo(tmp_path / "peer.git")
    peer_workspace = tmp_path / "peer-workspace"
    peer_workspace.mkdir()

    overlay_ref = OverlayRef(author="synapt", name="hindsight")

    capture_overlay_object(
        local_store,
        OVERLAY_SOURCE,
        _overlay_meta(overlay_ref),
    )
    push_overlay_ref(local_store, remote_store, overlay_ref)
    fetch_overlay_ref(peer_store, remote_store, overlay_ref)

    result = activate_overlay(
        workspace_root=peer_workspace,
        overlay_store=peer_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="synapt/hindsight",
        overlay_signer=None,
    )

    assert result.status == "ok"

    for filename in EXPECTED_FILES:
        target = peer_workspace / filename
        source = OVERLAY_SOURCE / filename
        assert target.read_text() == source.read_text(), (
            f"Content mismatch after push/fetch: {filename}"
        )


def test_hindsight_overlay_deactivate_restores_clean_workspace(tmp_path: Path) -> None:
    store = _init_bare_git_repo(tmp_path / "store.git")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _write_file(workspace / "local-notes.txt", "user notes\n")

    overlay_ref = OverlayRef(author="synapt", name="hindsight")
    capture_overlay_object(store, OVERLAY_SOURCE, _overlay_meta(overlay_ref))

    activate_overlay(
        workspace_root=workspace,
        overlay_store=store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="synapt/hindsight",
        overlay_signer=None,
    )

    assert (workspace / "settings.toml").exists()
    assert (workspace / "local-notes.txt").read_text() == "user notes\n"

    deactivate_overlay(workspace_root=workspace, overlay_ref=overlay_ref)

    assert not (workspace / "settings.toml").exists()
    assert not (workspace / "analysis.yml").exists()
    assert not (workspace / "COMPOSE.md").exists()
    assert (workspace / "local-notes.txt").read_text() == "user notes\n"
    assert read_active_overlay_stack(workspace) == []


def test_hindsight_overlay_settings_toml_has_required_fields(tmp_path: Path) -> None:
    import tomllib

    settings = OVERLAY_SOURCE / "settings.toml"
    with open(settings, "rb") as f:
        data = tomllib.load(f)

    assert data["overlay"]["name"] == "hindsight"
    assert data["overlay"]["repo"] == "vectorize-io/hindsight"
    assert data["analysis"]["benchmark"]["dataset"] == "LOCOMO"
    assert data["analysis"]["benchmark"]["baseline_score"] == 72.0
    assert data["analysis"]["benchmark"]["secondary_dataset"] == "LongMemEval"
    assert "mem0" in data["comparison"]["competitors"]
    assert "zep" in data["comparison"]["competitors"]


def test_hindsight_overlay_analysis_yml_has_module_entries_and_dual_eval() -> None:
    import yaml

    analysis = OVERLAY_SOURCE / "analysis.yml"
    with open(analysis) as f:
        data = yaml.safe_load(f)

    modules = data["modules"]
    assert len(modules) >= 4
    paths = [m["path"] for m in modules]
    assert "hindsight-api-slim/hindsight_api/engine/memory_engine.py" in paths
    assert "hindsight-api-slim/hindsight_api/engine/reflect/" in paths
    assert "hindsight-api-slim/hindsight_api/engine/consolidation/" in paths

    eval_cfg = data["evaluation"]
    assert eval_cfg["locomo"]["conversations"] == 10
    assert eval_cfg["locomo"]["gate_conversation"] == 3
    assert eval_cfg["longmemeval"]["enabled"] is True


def _overlay_meta(overlay_ref: OverlayRef) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author=overlay_ref.author,
        signature="unsigned",
        timestamp="2026-04-30T00:00:00Z",
        parent_overlay_refs=[],
    )


def _init_bare_git_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
