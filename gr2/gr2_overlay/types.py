"""Data types for the overlay substrate."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class OverlayTier(StrEnum):
    A = "config"


class TrustLevel(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class OverlayRef:
    """Reference to an overlay stored in refs/overlays/<author>/<name>."""

    author: str
    name: str

    @property
    def ref_path(self) -> str:
        return f"refs/overlays/{self.author}/{self.name}"


@dataclass
class OverlayMeta:
    """Metadata for a captured overlay."""

    ref: OverlayRef
    tier: OverlayTier
    trust: TrustLevel
    files: list[str] = field(default_factory=list)


@dataclass
class OverlayStackEntry:
    """One entry in the activation stack."""

    ref: OverlayRef
    priority: int
    active: bool


@dataclass
class MaterializeResult:
    """Result of eagerly materializing an overlay."""

    overlay: OverlayRef
    files_written: list[Path]
    conflicts: list[str]
    idempotent: bool
