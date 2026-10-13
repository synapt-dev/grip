"""Shared next steps for workspace layouts that gr2 cannot use yet."""

from pathlib import Path

from .layout import grip_dir

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


def missing_workspace_spec_message(workspace_root: Path) -> str:
    """One line for a verb that reads `.grip/workspace_spec.toml` on a root that has none: which kind of root it is,
    and the step that suits it. A native root is not told to migrate, and a gr1 desk is not told to init."""
    spec_path = grip_dir(workspace_root) / "workspace_spec.toml"
    if (workspace_root / "grip.toml").is_file():
        return (f"workspace spec not found: {spec_path}; {workspace_root} is a native gr2 root (grip.toml), "
                "and this verb reads a workspace spec, not grip.toml: it is not supported on a native root yet")
    if is_gr1_only_workspace(workspace_root):
        return (f"workspace spec not found: {spec_path}; {workspace_root} is still in gr1 format: run "
                f"`gr2 workspace migrate-gr1 {workspace_root}` to add gr2 alongside it; your gr1 setup keeps working")
    return (f"workspace spec not found: {spec_path}; run `gr2 workspace init {workspace_root}` first "
            "or create .grip/workspace_spec.toml explicitly")
