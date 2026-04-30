"""Workspace trust model: allowlist-based gating for overlay driver execution."""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from gr2_overlay.types import OverlayRef

CURATED_DRIVER_NAMES = frozenset({"overlay-deep", "overlay-prepend", "overlay-union"})


class TrustClass(StrEnum):
    LOCAL = "local"
    TEAM = "team"


@dataclass(frozen=True)
class TrustSource:
    kind: str
    pattern: str | None
    signer: str | None
    trust_class: TrustClass


class OverlayTrustError(Exception):
    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def trust_config_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "trust.toml"


def load_workspace_allowlist(workspace_root: Path) -> list[TrustSource]:
    config = trust_config_path(workspace_root)
    if not config.exists():
        return []

    data = tomllib.loads(config.read_text())
    sources = data.get("source", [])

    return [
        TrustSource(
            kind=s["kind"],
            pattern=s.get("pattern"),
            signer=s.get("signer"),
            trust_class=TrustClass(s["trust_class"]),
        )
        for s in sources
    ]


def write_workspace_allowlist(workspace_root: Path, sources: list[dict[str, Any]]) -> None:
    config = trust_config_path(workspace_root)
    config.parent.mkdir(parents=True, exist_ok=True)

    if not sources:
        config.write_text("")
        return

    lines: list[str] = []
    for source in sources:
        lines.append("[[source]]")
        lines.append(f'kind = "{source["kind"]}"')
        if "pattern" in source:
            lines.append(f'pattern = "{source["pattern"]}"')
        if "signer" in source:
            lines.append(f'signer = "{source["signer"]}"')
        lines.append(f'trust_class = "{source["trust_class"]}"')
        lines.append("")

    config.write_text("\n".join(lines) + "\n")


def authorize_overlay_driver(
    driver_name: str,
    overlay_ref: OverlayRef,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
    allowlist: list[TrustSource],
    declared_driver: str | None = None,
) -> TrustClass:
    if driver_name not in CURATED_DRIVER_NAMES:
        raise ValueError(f"Unknown overlay driver: {driver_name}")

    for source in allowlist:
        if _source_matches(source, overlay_source_kind, overlay_source_value, overlay_signer):
            return source.trust_class

    if declared_driver is not None:
        raise OverlayTrustError(
            "Overlay source not in allowlist; .gitattributes is metadata, not authority",
            error_code="overlay_untrusted",
        )

    raise OverlayTrustError(
        "Overlay source not in allowlist",
        error_code="overlay_untrusted",
    )


def _source_matches(
    source: TrustSource,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
) -> bool:
    if source.kind != overlay_source_kind:
        return False

    if source.kind == "path":
        if overlay_source_value is None or source.pattern is None:
            return False
        if ".." in overlay_source_value:
            return False
        return fnmatch.fnmatch(overlay_source_value, source.pattern)

    if source.kind == "signed":
        if overlay_signer is None or source.signer is None:
            return False
        return overlay_signer == source.signer

    return False


def can_inspect_overlay(
    overlay_ref: OverlayRef,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
    allowlist: list[TrustSource],
) -> bool:
    return True


def can_diff_overlay(
    overlay_ref: OverlayRef,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
    allowlist: list[TrustSource],
) -> bool:
    return True
