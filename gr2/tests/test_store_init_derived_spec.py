"""One init: a root made by `gr2 store init` works with `sync` at once, its workspace spec DERIVED from `grip.toml` in
memory on every read. No spec file is written, so `grip.toml` stays the one record of each member: an edit there is
what the next read sees. Before this change `sync status` refused ("run `gr2 workspace init <path>` first") until a
second init wrote a spec file, a copy that went stale at the first advance.

Each row names what turns it red:
  * R1 fresh store init, then sync status      -> the loader or sync's existence check does not derive the spec
  * R2 grip.toml edited after init is read     -> the derived spec is cached or written instead of read each time
  * R3 an existing spec file wins              -> the derived spec is preferred over a spec file someone declared
  * R4 the two spec loaders agree              -> lane_workspace_prototype.load_workspace_spec keeps its own copy
  * R5 a member with no origin is not derived   -> a repo is derived with an empty url
  * R6 a lane on a derived root writes no spec  -> lane create writes the spec file, or a unit other than default
                                                 is refused on a derived root
  * R7 consent reads the derived members       -> the sibling-member boundary is empty, or a member's consent key is
                                                 its checkout path, on a root with no spec file
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.spec_apply import workspace_spec_path
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

runner = CliRunner()


def _cli(*args: str):
    return runner.invoke(app, [*args], catch_exceptions=False)


def _remote(tmp_path: Path, name: str) -> Path:
    remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", remote)
    git(tmp_path, "clone", "-q", remote, seed)
    git(seed, "checkout", "-q", "-B", "main")
    (seed / "f").write_text(f"{name}\n")
    git(seed, "add", "f")
    git(seed, "commit", "-qm", name)
    git(seed, "push", "-q", "origin", "main")
    return remote


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    git(tmp_path, "clone", "-q", f"file://{_remote(tmp_path, 'alpha')}", root / "alpha")
    result = _cli("store", "init", str(root))
    assert result.exit_code == 0, result.output
    return root


def _synced_url(root: Path) -> str:
    """The url `sync status` plans to seed alpha's shared cache from: the spec it read."""
    result = _cli("sync", "status", str(root), "--json")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    return next(op["details"]["url"] for op in plan["operations"]
                if op["kind"] == "seed_repo_cache" and op["subject"] == "alpha")


def _set_origin(root: Path, url: str) -> None:
    text = (root / "grip.toml").read_text()
    lines = [line for line in text.splitlines() if line.startswith("origin = ")]
    assert len(lines) == 1, text
    (root / "grip.toml").write_text(text.replace(lines[0], f'origin = "{url}"'))


def test_a_fresh_store_init_root_syncs_with_no_second_init_and_no_spec_file(tmp_path):
    """R1."""
    root = _store(tmp_path)
    assert not workspace_spec_path(root).exists()
    status = _cli("sync", "status", str(root))
    assert status.exit_code == 0, status.output
    assert "workspace spec not found" not in status.output
    assert not workspace_spec_path(root).exists(), "sync wrote a spec file; the spec is derived, never written"


def test_an_edit_to_grip_toml_after_init_is_what_sync_reads(tmp_path):
    """R2, staleness. sync status is read once BEFORE the edit (so a derive that cached its answer, in memory or as a
    file, has something to go stale) and once after: the second read takes the remote grip.toml names now."""
    root = _store(tmp_path)
    assert _synced_url(root).endswith("/alpha.git")
    mirror = f"file://{_remote(tmp_path, 'mirror')}"
    _set_origin(root, mirror)
    assert _synced_url(root) == mirror
    assert not workspace_spec_path(root).exists()


def test_an_existing_spec_file_wins_and_is_left_alone(tmp_path):
    """R3. A spec file someone declared (here by `workspace init`, with a unit name store init would never choose) is
    the spec: sync reads ITS url after grip.toml's changes, and the file's bytes and mtime do not move."""
    root = _store(tmp_path)
    assert _cli("workspace", "init", str(root), "--default-unit", "declared").exit_code == 0
    spec = workspace_spec_path(root)
    before = spec.read_bytes()
    declared_url = _synced_url(root)
    os.utime(spec, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    _set_origin(root, f"file://{_remote(tmp_path, 'mirror')}")
    assert _synced_url(root) == declared_url
    assert spec.read_bytes() == before and b"declared" in before
    assert spec.stat().st_mtime_ns == 1_000_000_000_000_000_000


def test_the_two_spec_loaders_agree_on_a_derived_root(tmp_path):
    """R4. `lane_workspace_prototype.load_workspace_spec` (lanes, review checkout-pr) and `load_workspace_spec_doc`
    (sync, apply, validate) give the same spec for a store init root: the first delegates, so the seams cannot
    disagree about which root has a spec."""
    from gr2.prototypes import lane_workspace_prototype as lane_proto
    from gr2.python_cli import spec_apply

    root = _store(tmp_path)
    doc = spec_apply.load_workspace_spec_doc(root)
    assert lane_proto.load_workspace_spec(root) == doc
    assert [r["name"] for r in doc["repos"]] == ["alpha"] and doc["repos"][0]["url"].endswith("/alpha.git")


def test_a_member_with_no_origin_remote_is_not_derived(tmp_path):
    """R5. A spec repo needs a url. A grip.toml member with no `origin` remote gives the named refusal, as a root with
    no spec always has, rather than a derived repo with an empty url that nothing can clone."""
    from gr2.prototypes import lane_workspace_prototype as lane_proto
    from gr2.python_cli import spec_apply

    root = _store(tmp_path)
    text = (root / "grip.toml").read_text()
    lines = [line for line in text.splitlines() if line.startswith("origin = ")]
    assert len(lines) == 1
    (root / "grip.toml").write_text(text.replace(lines[0] + "\n", ""))
    assert not any(line.startswith("origin = ") for line in (root / "grip.toml").read_text().splitlines())
    with pytest.raises(lane_proto.WorkspaceSpecMissing):
        spec_apply.load_workspace_spec_doc(root)
    status = _cli("sync", "status", str(root))
    assert status.exit_code != 0 and "workspace spec not found" in status.output


def test_a_lane_on_a_derived_root_writes_no_spec_and_a_later_edit_is_read(tmp_path):
    """R6. `lane create` on a store init root, for a unit other than `default`, builds its lane from the derived spec
    and writes no spec file; a grip.toml edit made after the lane is what sync reads next. A lane that wrote the spec
    would bring back the second record: the file then wins and sync plans from the OLD origin."""
    root = _store(tmp_path)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "workspace")
    lane = _cli("lane", "create", str(root), "atlas", "work")
    assert lane.exit_code == 0, lane.output
    assert not workspace_spec_path(root).exists(), "lane create wrote a spec file on a derived root"
    mirror = f"file://{_remote(tmp_path, 'mirror')}"
    _set_origin(root, mirror)
    assert _synced_url(root) == mirror


def test_consent_reads_the_derived_members_on_a_root_with_no_spec_file(tmp_path):
    """R7. The sibling-member boundary (a member may not write into another member's tree) and the member consent key
    read the spec's [[repos]]. On a store init root they are the derived ones: alpha's boundary names beta, and a lane
    copy of alpha keys as `alpha`, as they would once a spec file existed."""
    from gr2.python_cli import consent

    root = tmp_path / "ws"
    root.mkdir()
    for name in ("alpha", "beta"):
        git(tmp_path, "clone", "-q", f"file://{_remote(tmp_path, name)}", root / name)
    assert _cli("store", "init", str(root)).exit_code == 0
    assert not workspace_spec_path(root).exists()
    assert consent.declared_member_roots(root, exclude=root / "alpha") == [(root / "beta").resolve()]
    lane_copy = root / ".grip" / "lanes" / "atlas" / "work" / "alpha"
    assert consent.member_key(root, lane_copy, "alpha") == "alpha"
