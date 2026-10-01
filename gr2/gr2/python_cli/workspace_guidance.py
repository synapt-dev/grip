"""Shared next steps for workspace layouts that gr2 cannot use yet."""

from pathlib import Path

GR1_ONLY_NEXT_STEP = (
    "This workspace is still in gr1 format. Run `gr2 workspace migrate-gr1 .` "
    "to add gr2 alongside it; your gr1 setup keeps working."
)


def is_gr1_only_workspace(workspace_root: Path) -> bool:
    """Whether this root has the gr1 manifest but no gr2 workspace spec."""
    return (workspace_root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").is_file() and not (
        workspace_root / ".grip" / "workspace_spec.toml"
    ).is_file()


def missing_gr2_workspace_guidance(workspace_root: Path, fallback: str) -> str:
    """Prefer the migration route only when there is actually gr1 state to migrate."""
    if is_gr1_only_workspace(workspace_root):
        return GR1_ONLY_NEXT_STEP
    return fallback
