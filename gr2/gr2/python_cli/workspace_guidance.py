"""Shared next steps for workspace layouts that gr2 cannot use yet."""

from pathlib import Path

from .layout import grip_dir
from .next_steps import _shown

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


def enclosing_workspace_root(path: Path) -> Path | None:
    """The nearest strict ancestor of `path` that is a workspace (a spec, or a native grip.toml root), else None.
    A verb pointed at a member or any directory inside a workspace is sent to that root, never told to init a
    second workspace nested inside the first."""
    for parent in path.resolve().parents:
        if (grip_dir(parent) / "workspace_spec.toml").is_file() or (parent / "grip.toml").is_file():
            return parent
    return None


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
    return f"workspace spec not found: {spec_path}; " + init_unless_inside_workspace(
        workspace_root, f"run {workspace_init_command(workspace_root)} first or create .grip/workspace_spec.toml explicitly")


def workspace_init_command(target: object = "") -> str:
    """The one place a verb's advice spells the init command (a structural test holds every other text to it), so
    whether that advice suits the path is decided once, in `init_unless_inside_workspace`."""
    return f"`gr2 workspace init {_shown(target)}`" if str(target) else "`gr2 workspace init`"


def inside_workspace_sentence(path: Path, enclosing: Path) -> str:
    return (f"{path} is inside the workspace at {enclosing}: "
            f"pass the workspace root {enclosing} (or run from there), not a directory inside it")


def init_unless_inside_workspace(workspace_root: Path, init_advice: str) -> str:
    """`init_advice` (a sentence naming `workspace_init_command`) for a root that may become a workspace; for a
    directory inside one (a member clone, a subdirectory), the way to that workspace's root instead, since init there
    would nest a second workspace inside the first. A root that is itself a workspace (a spec or a grip.toml) keeps
    its own advice, and a gr1 desk is sent to migrate first."""
    if (grip_dir(workspace_root) / "workspace_spec.toml").is_file() or (workspace_root / "grip.toml").is_file():
        return init_advice
    if is_gr1_only_workspace(workspace_root):
        return GR1_ONLY_NEXT_STEP
    enclosing = enclosing_workspace_root(workspace_root)
    if enclosing is not None:
        return inside_workspace_sentence(workspace_root, enclosing)
    return init_advice
