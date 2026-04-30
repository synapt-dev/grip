# gr2 overlay substrate

Config-overlay capture, composition, and materialization for gitgrip
workspaces. Overlays are git-native objects that layer configuration
files on top of a workspace without modifying the base branch.

## What is an overlay?

An overlay is a set of Tier A config files (`.toml`, `.yml`, `.json`,
`COMPOSE.md`) captured as an annotated git tag pointing at a structured
tree. Overlays live in `refs/overlays/<author>/<name>` and can be pushed,
fetched, and activated across machines using standard git transport.

When activated, an overlay eagerly materializes its files into the
workspace. When deactivated, those files are removed and the workspace
returns to its pre-overlay state.

## Quick start

Create your first overlay in under 3 minutes.

### 1. Set up stores

```python
from pathlib import Path
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.activate import activate_overlay, deactivate_overlay
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel

# Initialize a bare git repo as the overlay store
import subprocess
store = Path("overlay-store.git")
subprocess.run(["git", "init", "--bare", str(store)], check=True)
```

### 2. Capture an overlay

```python
# Create source config files
source = Path("my-overlay-source")
source.mkdir(exist_ok=True)
(source / "settings.toml").write_text('[ui]\ntheme = "dark"\n')
(source / "COMPOSE.md").write_text("# My Overlay\n")

# Capture into the store
ref = OverlayRef(author="myteam", name="dark-theme")
meta = OverlayMeta(
    ref=ref,
    tier=OverlayTier.A,
    trust=TrustLevel.TRUSTED,
    author="myteam",
    timestamp="2026-05-01T00:00:00Z",
)
capture_overlay_object(store, source, meta)
```

### 3. Activate in a workspace

```python
workspace = Path("my-workspace")
workspace.mkdir(exist_ok=True)

result = activate_overlay(
    workspace_root=workspace,
    overlay_store=store,
    overlay_ref=ref,
    overlay_source_kind="path",
    overlay_source_value="myteam/dark-theme",
    overlay_signer=None,
)

assert (workspace / "settings.toml").read_text() == '[ui]\ntheme = "dark"\n'
```

### 4. Deactivate

```python
deactivate_overlay(workspace_root=workspace, overlay_ref=ref)
assert not (workspace / "settings.toml").exists()
```

## Multi-machine distribution

Overlays use identity-mapped refspecs for transport between stores:

```python
from gr2_overlay.refs import push_overlay_ref, fetch_overlay_ref

# Machine A: push to a shared remote
push_overlay_ref(local_store, remote_store, overlay_ref)

# Machine B: fetch and activate
fetch_overlay_ref(peer_store, remote_store, overlay_ref)
activate_overlay(
    workspace_root=peer_workspace,
    overlay_store=peer_store,
    overlay_ref=overlay_ref,
    overlay_source_kind="path",
    overlay_source_value="myteam/dark-theme",
    overlay_signer=None,
)
```

## Curated merge drivers

Three drivers handle file composition when overlays interact with
existing workspace content:

| Driver | Behavior | File types |
|--------|----------|------------|
| `overlay-deep` | Recursive dict merge; overlay wins conflicts | `.toml`, `.yml`, `.json` |
| `overlay-prepend` | Concatenates overlay content before base | Any text file |
| `overlay-union` | Deduplicated line set union | Line-oriented files |

Drivers are declared in `.gitattributes`:

```
*.toml merge=overlay-deep
*.yml merge=overlay-deep
*.json merge=overlay-deep
COMPOSE.md merge=overlay-prepend
```

## Trust model

Activation is gated by a workspace-local allowlist at
`.grip/trust.toml`. If no trust config exists, the workspace operates
in **open mode** (all overlays are accepted).

```toml
[[sources]]
kind = "path"
pattern = "myteam/*"
trust_class = "local"

[[sources]]
kind = "path"
pattern = "vendor/*"
trust_class = "team"

[[sources]]
kind = "signed"
signer = "release-bot@myorg.com"
trust_class = "team"
```

Trust classes:
- **local**: overlays from your own team or workspace
- **team**: overlays from verified external sources

Untrusted overlays are blocked before any files are written.
Path traversal patterns (containing `..`) are always rejected.

`.gitattributes` is treated as metadata, not authority: it declares
which driver to use but does not bypass the trust allowlist.

## Introspection

Five query functions provide visibility into overlay state. Each
supports both human-readable and machine-readable (`--json`) output:

| Function | Purpose |
|----------|---------|
| `overlay_stack` | Active and available overlays with author metadata |
| `overlay_status` | Active, available, and applied overlay sets |
| `overlay_trace` | Per-file line-region attribution to overlay refs |
| `overlay_why` | Winning merge rule and reason for a file |
| `overlay_impact` | Files touched by an overlay (reads git objects directly) |

## Performance gates

The perf harness (`gr2_overlay/perf.py`) measures overlay operations
against git baselines:

| Gate | Measures | Threshold |
|------|----------|-----------|
| `activate_vs_git_checkout_single_file` | Activate latency vs `git checkout` | 2.0x |
| `status_vs_git_status` | Overlay status vs `git status` | 2.0x |
| `diff_vs_git_diff` | Overlay diff vs `git diff` | 2.0x |

Gates use median-based sampling to reduce single-run noise.

## WorkspaceSpec

Overlays are declared in `.grip/overlays.toml`:

```toml
[[overlays]]
name = "dark-theme"
path = "refs/overlays/myteam/dark-theme"
applies_to = ["*.toml", "*.yml"]
priority = 10

[[overlays]]
name = "shared-base"
path = "refs/overlays/team/shared-base"
applies_to = ["*.toml"]
priority = 0
```

Higher priority overlays take precedence during composition.

## Object encoding

Each overlay is stored as an annotated tag pointing at a structured
tree with four entries:

```
<tag>
  └── <tree>
      ├── metadata_blob     # TOML metadata (author, tier, trust, timestamp)
      ├── staged_index_tree  # Files from the staging area
      ├── untracked_blobs    # Untracked file content
      └── working_tree_tree  # Complete working tree snapshot
```

The Tier A filter captures only config files: `.toml`, `.yml`, `.json`
by extension, plus `COMPOSE.md` by exact filename.

## M1 acceptance criteria

1. Capture Tier A overlay from source directory into bare git store
2. Push overlay ref to remote store via identity-mapped refspec
3. Fetch overlay ref on a separate store (simulating a different machine)
4. Activate overlay: eagerly materialize all files into workspace
5. Verify byte-for-byte content fidelity across all Tier A file types
6. Deactivate: remove all overlay files, clean empty parent directories
7. Verify workspace returns to pre-overlay state
8. Trust gating: block untrusted overlays before any file writes
9. Open mode: no trust config permits all overlays
10. Idempotent: activate/deactivate cycles produce identical state

All criteria are validated by the acceptance harness at
`tests/test_overlay_acceptance.py`.

## Module reference

| Module | Purpose |
|--------|---------|
| `types.py` | Data types: `OverlayRef`, `OverlayMeta`, `OverlayTier`, `TrustLevel` |
| `objects.py` | Capture and apply overlay objects (git plumbing) |
| `refs.py` | Push and fetch overlay refs between stores |
| `drivers.py` | Curated merge drivers (deep, prepend, union) |
| `trust.py` | Workspace trust model and allowlist |
| `activate.py` | Activate and deactivate overlays |
| `introspection.py` | Query functions (stack, status, trace, why, impact) |
| `workspace_spec.py` | `[[overlays]]` TOML schema |
| `perf.py` | Performance gates measurement harness |
| `cli.py` | Typer CLI stubs for `gr overlay` subcommands |

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check gr2_overlay/ tests/
ruff format --check gr2_overlay/ tests/
```
