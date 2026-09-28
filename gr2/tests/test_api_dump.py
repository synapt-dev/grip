"""The two gates on gr2's committed public-surface dump.

- ``test_api_dump_is_current``: the committed ``api/cli.api`` must equal what
  the generator renders now. A change to the public surface fails here unless
  the file changes in the same PR.
- ``test_api_stable_share_at_least_90``: stable items over all non-reserved,
  non-hidden items must be at least 0.90, with both counts printed. The lever
  is hiding our internal process verbs, not freezing everything: hidden items
  leave the denominator, which is why the hide list is load-bearing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
CLI_API = GR2 / "api" / "cli.api"
GENERATOR = GR2 / "scripts" / "dump_api.py"


def _generator():
    """Load the generator by path -- it is a script, not an installed module."""
    spec = importlib.util.spec_from_file_location("_gr2_dump_api", GENERATOR)
    assert spec and spec.loader, f"cannot load {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(text: str) -> list[tuple[str, str, str]]:
    """(kind, label, marker) for every line, parsed from the two fixed columns."""
    rows = []
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        label, _, marker = rest.rstrip().rpartition(" ")
        rows.append((kind, label.strip(), marker))
    return rows


def test_api_dump_is_current() -> None:
    rendered = _generator().render()
    committed = CLI_API.read_text() if CLI_API.exists() else ""
    assert committed == rendered, (
        f"{CLI_API} is not current.\n"
        f"  regenerate: python scripts/dump_api.py\n"
        f"  then name the public-surface change in the PR body.\n"
        f"  committed lines={len(committed.splitlines())} "
        f"rendered lines={len(rendered.splitlines())}"
    )


def test_api_stable_share_at_least_90() -> None:
    rows = _rows(_generator().render())
    counted = [
        (kind, label, marker)
        for kind, label, marker in rows
        if marker != "reserved" and not label.endswith("(hidden)")
    ]
    stable = [row for row in counted if row[2] == "stable"]
    share = len(stable) / len(counted) if counted else 1.0
    print(f"stable share: {len(stable)}/{len(counted)} = {share:.4f}")
    assert share >= 0.90, (
        f"stable share {len(stable)}/{len(counted)} = {share:.4f} is under 0.90.\n"
        f"  hide the internal verbs (hidden items leave the denominator) or mark the\n"
        f"  moving ones may-change in api/stability.toml; do not mark them stable."
    )
