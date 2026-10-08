"""Inferred binds share the workspace member loader's pin and location contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_review_a4_v2 import _init, _invoke, _refs, _assert_only_bind_stdout
from tests.test_review_a4 import _flat
from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import two_member_ws  # noqa: F401


def _spec(ws: Path, remote: str, pin: str | None, **fields: str) -> None:
    spec = ws / ".grip" / "workspace_spec.toml"
    spec.parent.mkdir(exist_ok=True)
    row = {"name": "alpha", "url": remote, **fields}
    if pin is not None:
        row["pin"] = pin
    spec.write_text('workspace_name = "ws"\n\n[[repos]]\n' +
                    "\n".join(f"{key} = {json.dumps(value)}" for key, value in row.items()) + "\n")


@pytest.mark.parametrize("pins", ["conflict", "manifest-only", "agree"])
def test_inferred_bind_uses_shared_pin_merge(two_member_ws: Path, pins: str) -> None:
    ws = two_member_ws
    _init(ws)
    remote, base, head = _unpushed_head(ws)
    _spec(ws, remote, None if pins == "manifest-only" else base, path="alpha", ref="main")
    if pins == "conflict":
        manifest = ws / "grip.toml"
        original = manifest.read_text()
        assert original.count(base) == 1
        manifest.write_text(original.replace(base, head))
    print(f"SUBJECT root={ws} mode={pins} base={base} head={head}")
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws))
    if pins == "conflict":
        assert result.exit_code == 2, result.output
        text = _flat(result.stderr)
        assert "workspace_spec.toml" in text and "grip.toml" in text
        assert base[:12] in text and head[:12] in text and "must agree" in text
        assert result.stdout == "" and "Traceback" not in result.stderr
        assert _refs(ws) == before
    else:
        gr_id = _assert_only_bind_stdout(result)
        shown = _invoke("review", "show", str(ws), gr_id, "--json")
        assert shown.exit_code == 0, shown.output
        [row] = json.loads(shown.stdout)["members"]
        assert (row["key"], row["base"], row["head"]) == ("alpha", base, head)
        verified = _invoke("review", "verify", str(ws), gr_id, "--json")
        assert verified.exit_code == 0 and json.loads(verified.stdout)["tree_matches"] is True


@pytest.mark.parametrize("source", ["spec", "manifest"])
def test_inferred_member_path_and_qualified_ref(two_member_ws: Path, source: str) -> None:
    ws = two_member_ws
    _init(ws)
    remote, base, head = _unpushed_head(ws)
    destination = ws / "members" / "alpha"
    destination.parent.mkdir()
    (ws / "alpha").rename(destination)
    if source == "spec":
        _spec(ws, remote, base, path="members/alpha", ref="refs/heads/main")
    else:
        manifest = ws / "grip.toml"
        text = manifest.read_text()
        assert text.count('path = "alpha"') == 1
        blocks = text.split("[[members]]")
        for i, block in enumerate(blocks[1:], start=1):
            if 'path = "alpha"' in block:
                block = block.replace('path = "alpha"', 'path = "members/alpha"')
                lines = block.splitlines()
                assert sum(line.startswith("ref = ") for line in lines) == 1
                blocks[i] = "\n".join('ref = "refs/heads/main"' if line.startswith("ref = ") else line for line in lines) + "\n"
        manifest.write_text("[[members]]".join(blocks))
        _spec(ws, remote, None)
    result = _invoke("review", "bind", str(ws))
    gr_id = _assert_only_bind_stdout(result)
    shown = _invoke("review", "show", str(ws), gr_id, "--json")
    assert shown.exit_code == 0, shown.output
    [row] = json.loads(shown.stdout)["members"]
    assert (row["path"], row["base"], row["head"]) == ("members/alpha", base, head)


def test_members_exclusion_is_named_on_stderr(two_member_ws: Path) -> None:
    ws = two_member_ws
    _init(ws)
    _unpushed_head(ws)
    result = _invoke("review", "bind", str(ws), "--members", "alpha")
    gr_id = _assert_only_bind_stdout(result)
    assert "beta (excluded by --members)" in result.stderr
    shown = _invoke("review", "show", str(ws), gr_id, "--json")
    assert shown.exit_code == 0 and [m["key"] for m in json.loads(shown.stdout)["members"]] == ["alpha"]
