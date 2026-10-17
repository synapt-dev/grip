"""Section 9, fence row (g): a hand commit that deletes a member's `pin = ...` line from grip.toml (keeping its name)
is refused BY NAME, exit 4, at commit (the hook) and at push (past the hook), in both modes; never a traceback.
`_recorded_pins` reads every [[members]] entry's pin, so a pin-less member must be named before it is read."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import MODES, SHADOW, _refs, _store


def _drop_pin_line(root: Path, member: str) -> None:
    """Delete `member`'s pin line from grip.toml BY HAND and stage it; the member's name and path stay."""
    toml = root / "grip.toml"
    head, block = toml.read_text(encoding="utf-8").split(f'name = "{member}"', 1)
    lines = block.split("\n")
    kept = [line for line in lines if not line.strip().startswith("pin =")] if "[[members]]" not in block else None
    if kept is None:
        own, rest = block.split("[[members]]", 1)
        own = "\n".join(line for line in own.split("\n") if not line.strip().startswith("pin ="))
        block = own + "[[members]]" + rest
    else:
        block = "\n".join(kept)
    toml.write_text(head + f'name = "{member}"' + block, encoding="utf-8")
    assert f'name = "{member}"' in toml.read_text(encoding="utf-8")
    git(root, "add", "grip.toml")


@pytest.mark.parametrize("mode", MODES)
def test_fence_g_pin_line_removed_by_hand_is_refused_at_commit_naming_the_member(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    head, local, remote = git(root, "rev-parse", "HEAD").stdout.strip(), _refs(root), _refs(origin)
    _drop_pin_line(root, "alpha")

    refused = git(root, "commit", "-m", "hand drop pin", check=False)

    assert refused.returncode != 0
    assert "Traceback" not in refused.stderr, refused.stderr
    assert "pin line removed by hand" in refused.stderr and "alpha" in refused.stderr, refused.stderr
    assert git(root, "rev-parse", "HEAD").stdout.strip() == head and _refs(root)[SHADOW] == local[SHADOW]
    assert _refs(origin) == remote


@pytest.mark.parametrize("mode", MODES)
def test_fence_g_pin_line_removed_by_hand_is_refused_at_push_naming_the_member(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    shadow, remote = _refs(root)[SHADOW], _refs(origin)
    _drop_pin_line(root, "alpha")
    git(root, "commit", "--no-verify", "-m", "hand drop pin past the hook")

    result = gr2(root, "push", check=False)

    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 4, (result.returncode, result.stderr)
    assert "pin line removed by hand" in result.stderr and "alpha" in result.stderr, result.stderr
    assert _refs(origin) == remote, "a fenced push moves neither ref on origin"
    assert _refs(root)[SHADOW] == shadow, "nor the local shadow"
