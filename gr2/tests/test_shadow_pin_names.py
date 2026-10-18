"""A shadow's pins tree is one git fsck must accept: a pin whose path would make an entry fsck rejects is
refused by name, escaped, before any tree is written."""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import shadow as sh


def _git(repo, *args, data=b""):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          input=data, capture_output=True, check=True).stdout.decode()


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    blob = _git(repo, "hash-object", "-w", "--stdin", data=b"x").strip()
    base = _git(repo, "mktree", data=f"100644 blob {blob}\tf\n".encode()).strip()
    member = _git(repo, "commit-tree", base, "-m", "m").strip()
    return repo, base, member


def _refused(world, pins):
    repo, base, member = world
    with pytest.raises(ValueError) as caught:
        sh._tree_with_pins(repo, base, dict.fromkeys(pins, member))
    return str(caught.value)


def test_pins_fsck_accepts_are_built(world):
    repo, base, member = world
    names = ["ok/b", ".github", ".gitx", "gits", "git~2", "a.git", ".git~1", "x/.gitmodulesx", "gitmod~5", "gi7eba~x", ".gitattribute", "x/.g\u200cithub",
             "x/.git.x", "x/git~1x"]
    tree = sh._tree_with_pins(repo, base, dict.fromkeys(names, member))
    _git(repo, "update-ref", "refs/heads/t", _git(repo, "commit-tree", tree, "-m", "s").strip())
    fsck = subprocess.run(["git", "-C", str(repo), "fsck", "--strict", "--no-dangling"], capture_output=True,
                          text=True)
    assert fsck.returncode == 0, fsck.stderr
    listed = _git(repo, "ls-tree", "-r", "-z", "--name-only", tree, "--", sh.PINS).split("\0")
    assert sorted(n for n in listed if n) == sorted(f"{sh.PINS}/{n}" for n in names)


@pytest.mark.parametrize("pin", ['".git"', 'a/".GIT"', '".gitmodules"', "a/b\nc", '"\\056git"'])
def test_a_pin_is_written_by_its_literal_name(world, pin):
    """mktree in text mode C-unquotes a name that starts with a quote ('".git"' would become .git); each tree is
    written NUL-terminated, so the name fsck_bad_name checked is the name in the tree."""
    repo, base, member = world
    tree = sh._tree_with_pins(repo, base, {pin: member})
    _git(repo, "update-ref", "refs/heads/t", _git(repo, "commit-tree", tree, "-m", "s").strip())
    fsck = subprocess.run(["git", "-C", str(repo), "fsck", "--strict", "--no-dangling"], capture_output=True,
                          text=True)
    assert fsck.returncode == 0, fsck.stderr
    listed = _git(repo, "ls-tree", "-r", "-z", "--name-only", tree, "--", sh.PINS).split("\0")
    assert [n for n in listed if n] == [f"{sh.PINS}/{pin}"]


@pytest.mark.parametrize("pin", ["/x", "a/", "a//b", ""])
def test_a_pin_with_an_empty_entry_is_refused(world, pin):
    message = _refused(world, [pin])
    assert f"pin {pin!r}" in message and "emptyName" in message


