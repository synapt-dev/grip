"""Resolver: multi-overlay composition engine.

Post-activation step that composes overlapping files through merge drivers.
Design contract: config#196 (resolver-materialization-contract-2026-05-15.md).

Stub module: provides API surface for TDD spec collection.
Implementation lands in a follow-on PR once this spec ratifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ResolveError:
    file_path: str
    driver: str
    error: str


@dataclass
class ResolveResult:
    resolved_files: list[str] = field(default_factory=list)
    passthrough_files: list[str] = field(default_factory=list)
    errors: list[ResolveError] = field(default_factory=list)


def resolve_stack(
    workspace_root: Path,
    overlay_store: Path,
) -> ResolveResult:
    raise NotImplementedError


def _find_overlapping_files(
    workspace_root: Path,
    overlay_store: Path,
    stack: list[str],
) -> dict[str, list[str]]:
    raise NotImplementedError


def _get_driver_for_file(
    workspace_root: Path,
    file_path: str,
) -> str | None:
    raise NotImplementedError
