"""A bind on a root that is still a gr1 workspace refuses; it never migrates the root as a side effect.

A writer (`review bind` / `review pin`, `review create-project`) on a root with no store sets up the
native store itself, so a stranger who ran `workspace init` needs no `store init` step. A root that
holds a gr1 manifest is different: setting up a store there converts an existing workspace (a root
.git, grip.toml, .gitinclude, a generated .gitignore, an empty .grip/.git moved aside). That is a
migration decided per root, so the bind refuses (exit 2, `gr1_root`), writes nothing, and names
`gr2 store init`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gr2.python_cli import grip as grip_mod
from gr2.python_cli.migration import gr1_manifest_path
from tests.test_review_bind_native_store import _bind_args, _spec_only_root, _unpushed_head
from tests.test_store_break_attempts import _cli, two_member_ws  # noqa: F401


def _gr1_manifest(ws: Path) -> Path:
    path = gr1_manifest_path(ws)
    path.parent.mkdir(parents=True)
    path.write_text("version: 2\nrepos: {}\n")
    return path


def _snapshot(ws: Path) -> dict[str, tuple[int, int]]:
    """Every path at the root outside the members, with its size and mtime: a refusal changes none."""
    out = {}
    for path in sorted(ws.rglob("*")):
        rel = path.relative_to(ws)
        if rel.parts[0] in ("alpha", "beta"):
            continue
        st = path.lstat()
        out[rel.as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def test_bind_on_a_coexistence_root_refuses_and_writes_nothing(two_member_ws: Path) -> None:
    ws = two_member_ws
    _gr1_manifest(ws)
    _spec_only_root(ws)
    assert gr1_manifest_path(ws).exists() and (ws / ".grip" / "workspace_spec.toml").exists()  # coexistence
    remote, base, head = _unpushed_head(ws)
    before = _snapshot(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))

    assert code == 2 and "gr1_root" in out, out
    assert "gr2 store init" in out and "nothing was written" in out, out
    assert "set up a native store" not in out, out
    assert _snapshot(ws) == before
    assert not (ws / ".git").exists() and not (ws / "grip.toml").exists()


def test_bind_on_a_gr1_root_with_an_empty_alpha_store_refuses_and_keeps_it_in_place(two_member_ws: Path) -> None:
    """gr1-only: a gr1 manifest beside the empty `.grip/.git` an older workspace init left."""
    ws = two_member_ws
    _gr1_manifest(ws)
    grip_mod.grip_init(ws)
    assert gr1_manifest_path(ws).exists() and not (ws / ".grip" / "workspace_spec.toml").exists()  # gr1-only
    remote, base, head = _unpushed_head(ws)
    before = _snapshot(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))

    assert code == 2 and "gr1_root" in out and "moves .grip/.git aside" in out, out
    assert _snapshot(ws) == before
    assert (ws / ".grip" / ".git").is_dir() and not (ws / ".grip" / "legacy-store.git").exists()
    assert not (ws / ".git").exists() and not (ws / "grip.toml").exists()


def test_bind_on_a_gr2_only_root_still_sets_up_the_store(two_member_ws: Path, capfd) -> None:
    """The stranger path stays one step: no gr1 manifest, so the bind sets up the store itself."""
    ws = two_member_ws
    _spec_only_root(ws)
    assert not gr1_manifest_path(ws).exists()  # gr2-only
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))

    assert code == 0, out
    assert "set up a native store" in out + capfd.readouterr().err
    assert (ws / ".git").exists() and (ws / "grip.toml").is_file()


@pytest.mark.parametrize("as_file", [".gitgrip", ".gitgrip/spaces"])
def test_a_gitgrip_path_that_is_a_plain_file_is_not_a_gr1_manifest(two_member_ws: Path, capfd, as_file: str) -> None:
    """A `.gitgrip` (or `.gitgrip/spaces`) that is a regular file holds no gr1 manifest (lstat says ENOTDIR), so the
    root is gr2-only and the bind sets up the store as it does for any root with no gr1 manifest."""
    ws = two_member_ws
    target = ws / as_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not a directory\n")
    _spec_only_root(ws)
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))

    assert code == 0, out
    assert "set up a native store" in out + capfd.readouterr().err
    assert (ws / ".git").exists() and (ws / "grip.toml").is_file()
    assert target.read_text() == "not a directory\n"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads through a 000 directory")
@pytest.mark.skipif(os.name == "nt", reason="chmod 000 does not deny a directory search on Windows")
def test_a_gr1_manifest_the_bind_cannot_read_counts_as_present(two_member_ws: Path) -> None:
    """When the check cannot tell (the gr1 directory cannot be searched), it refuses rather than migrate."""
    ws = two_member_ws
    manifest = _gr1_manifest(ws)
    _spec_only_root(ws)
    remote, base, head = _unpushed_head(ws)
    locked = ws / ".gitgrip"
    locked.chmod(0)
    try:
        # precondition: the manifest cannot be stat'ed (Path.exists() raises here on 3.13; os.path.exists says False)
        assert not os.path.exists(manifest)
        code, out = _cli(*_bind_args(ws, remote, base, head))
    finally:
        locked.chmod(0o755)

    assert code == 2 and "gr1_root" in out, out
    assert not (ws / ".git").exists() and not (ws / "grip.toml").exists()


def test_create_project_on_a_coexistence_root_refuses_and_writes_nothing(two_member_ws: Path) -> None:
    """The other writer that sets up a store: the engine call refuses before it reads its pins."""
    ws = two_member_ws
    _gr1_manifest(ws)
    _spec_only_root(ws)
    before = _snapshot(ws)

    with pytest.raises(grip_mod.Gr1RootRefused, match="gr2 store init"):
        grip_mod.create_project_review_commit(ws, [])

    assert _snapshot(ws) == before
    assert not (ws / ".git").exists() and not (ws / "grip.toml").exists()
