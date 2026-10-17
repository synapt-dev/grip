"""Ref names holding a Unicode line separator (U+2028, U+0085) read as one name.

git splits ls-remote and ls-tree output on "\\n" only; str.splitlines also splits on
U+2028/U+0085, cutting one line in two and failing the unpack.

Rows:
L1 check_members reads a pinned member named with the separator, under quotePath true and false:
   absent on the remote it is refused naming the member; present at the pin it passes;
L2 delete reads an ls-remote listing that holds a ref with the separator, and deletes its record.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from tests.test_delete import ORG, dl, git, push, shadow, stook, world, _identity  # noqa: F401

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])


@SEPS
@pytest.mark.parametrize("quote_path", ["true", "false"])
def test_l1_check_members_names_a_member_with_a_line_separator(tmp_path, sep, quote_path):
    arbon = importlib.import_module("gr2.python_cli.arbon")
    sh = importlib.import_module("gr2.python_cli.shadow")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "config", "core.quotePath", quote_path)
    mk = lambda data, *a: subprocess.run(["git", "-C", str(repo), *a], input=data, capture_output=True,
                                         check=True).stdout.decode().strip()
    blob = mk(b"x", "hash-object", "-w", "--stdin")
    member = git(repo, "commit-tree", mk(f"100644 blob {blob}\tf\n".encode(), "mktree"), "-m", "m")
    path = f"a{sep}x"
    pins = mk(f"160000 commit {member}\t{path}\0".encode(), "mktree", "-z")
    shadow_commit = git(repo, "commit-tree", mk(f"040000 tree {pins}\t{sh.PINS}\0".encode(), "mktree", "-z"),
                        "-m", "s")
    with pytest.raises(arbon.MemberNotCurrent) as refused:
        arbon.check_members(repo, shadow_commit, org="org")
    assert (refused.value.member, refused.value.pin, refused.value.now) == (path, member, None)
    git(repo, "push", "-q", "origin", f"{member}:refs/org/{path}/STOOK")
    arbon.check_members(repo, shadow_commit, org="org")


@SEPS
def test_l2_delete_reads_a_listing_with_a_line_separator_ref(dl, world, sep):  # noqa: F811
    origin, me = world
    stray = f"refs/{ORG}/__agents__/c{sep}d/STOOK"
    git(me, "push", "-q", "origin", f"{stook('b')}:{stray}")
    git(me, "fetch", "-q", "origin")
    assert dl.delete(me, stook("a"), org=ORG) == [stook("a"), shadow("a")]
    assert git(origin, "for-each-ref", "--format=%(refname)", stray) == stray
