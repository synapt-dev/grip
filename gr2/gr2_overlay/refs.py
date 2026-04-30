"""Overlay ref transport: push and fetch overlay refs between bare stores."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.types import OverlayRef


def push_overlay_ref(
    overlay_store: Path,
    remote_store: Path,
    overlay_ref: OverlayRef,
) -> None:
    refspec = f"{overlay_ref.ref_path}:{overlay_ref.ref_path}"
    subprocess.run(
        ["git", f"--git-dir={overlay_store}", "push", str(remote_store), refspec],
        check=True,
        capture_output=True,
        text=True,
    )


def fetch_overlay_ref(
    overlay_store: Path,
    remote_store: Path,
    overlay_ref: OverlayRef,
) -> None:
    refspec = f"{overlay_ref.ref_path}:{overlay_ref.ref_path}"
    subprocess.run(
        ["git", f"--git-dir={overlay_store}", "fetch", str(remote_store), refspec],
        check=True,
        capture_output=True,
        text=True,
    )
