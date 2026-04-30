# gr2 overlay substrate

Config-overlay capture, composition, and materialization for gitgrip workspaces.

## M1 scope

- **Tier A only**: config files (`.toml`, `.yml`, `.json`)
- **Eager materialization**: overlays write files into the workspace on activation
- **Trust-gated**: overlays are classified as trusted or untrusted; untrusted overlays require explicit approval

## Status

Story 1/12: package skeleton and CLI stub scaffolding. All subcommands return "not implemented" and exit 1.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check gr2_overlay/ tests/
ruff format --check gr2_overlay/ tests/
```
