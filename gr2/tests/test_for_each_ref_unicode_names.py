"""for-each-ref names that hold U+2028 or U+0085 are read whole (gitops.for_each_ref_rows), not cut by str.split().

Rows:
U1 delete: a tracked SHADOW the remote no longer has is a stale view, refused with the same typed PushRefused
   whatever its name holds; the remote's refs are unchanged;
U2 _remote_branches_holding: a remote branch whose name holds the separator is returned whole, beside origin/main.
"""
from __future__ import annotations

import importlib
import subprocess

import pytest

from tests.test_delete import ORG, dl, git, on, stook, world  # noqa: F401

TRACK = f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/"
NAMES = pytest.mark.parametrize("sep", ["", "\u2028", "\u0085"], ids=["control", "U+2028", "U+0085"])


@NAMES
def test_u1_a_tracked_shadow_gone_from_the_remote_is_refused_as_stale_whatever_its_name_holds(dl, world, sep):
    origin, me = world
    agent = f"q{sep}r"
    git(me, "update-ref", f"{TRACK}{agent}/SHADOW", git(me, "rev-parse", f"{TRACK}a/SHADOW"))
    lp = importlib.import_module("gr2.python_cli.leased_push")
    before = on(origin)
    with pytest.raises(lp.PushRefused) as e:
        dl.delete(me, stook("a"), org=ORG)
    assert e.value.ref == f"refs/{ORG}/__agents__/{agent}/SHADOW"
    assert "stale tracking: the remote has nothing" in e.value.why and on(origin) == before


@NAMES
def test_u2_a_remote_branch_holding_the_pin_is_named_whole(tmp_path, sep):
    cli = importlib.import_module("gr2.python_cli.grip_cli")
    r = tmp_path / "m"
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    git(r, "commit", "-q", "--allow-empty", "-m", "x")
    sha = git(r, "rev-parse", "HEAD")
    for branch in (f"fe{sep}at", "main"):
        git(r, "update-ref", f"refs/remotes/origin/{branch}", sha)
    assert sorted(cli._remote_branches_holding(r, sha)) == sorted([f"origin/fe{sep}at", "origin/main"])
