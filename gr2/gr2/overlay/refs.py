"""Overlay ref transport: push and fetch overlay refs between bare stores."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.overlay.types import OverlayRef
from gr2.python_cli import gitops


def push_overlay_ref(
    overlay_store: Path,
    remote_store: Path,
    overlay_ref: OverlayRef,
) -> None:
    refspec = f"{overlay_ref.ref_path}:{overlay_ref.ref_path}"
    gitops.check(gitops.run_argv(["git", f"--git-dir={overlay_store}", "push", str(remote_store), refspec]))


def fetch_overlay_ref(
    overlay_store: Path,
    remote_store: Path,
    overlay_ref: OverlayRef,
) -> None:
    refspec = f"{overlay_ref.ref_path}:{overlay_ref.ref_path}"
    gitops.check(gitops.run_argv(["git", f"--git-dir={overlay_store}", "fetch", str(remote_store), refspec]))
