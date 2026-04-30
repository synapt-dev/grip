"""Data types for the overlay substrate."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class OverlayTier(StrEnum):
    A = "config"
    B = "source"


class TrustLevel(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class OverlayRef:
    """Reference to an overlay stored in refs/overlays/<author>/<name>."""

    author: str
    name: str

    @classmethod
    def parse(cls, ref_str: str) -> OverlayRef:
        parts = ref_str.strip().split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Invalid overlay ref '{ref_str}': expected '<author>/<name>'")
        return cls(author=parts[0], name=parts[1])

    @property
    def ref_path(self) -> str:
        return f"refs/overlays/{self.author}/{self.name}"


@dataclass
class OverlayMeta:
    """Metadata for a captured overlay."""

    ref: OverlayRef
    tier: OverlayTier
    trust: TrustLevel
    author: str = ""
    signature: str = "unsigned"
    timestamp: str = ""
    parent_overlay_refs: list[str] = field(default_factory=list)
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
