"""The layer size read stays local too. Scalar blobs are fetched first, so the
read reaches reader.size on the absent layer blob; every carried row is refused earlier, at the first scalar blob."""
from __future__ import annotations

import re
import subprocess

import pytest

from gr2.python_cli import member_field_tree as mft
from tests.test_review_transport import git


def test_layer_size_read_refuses_the_absent_layer_without_fetching(tmp_path):
    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    layer = b"# v2 git bundle\nlayer only\n"
    record = dict(kind=mft.KIND_REPO, pin="a" * 40, url="https://example.com/x.git", ref="main", layers=[layer])
    git(remote, "update-ref", "refs/heads/main", mft.commit(remote, record, "record"))
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    tree = git(remote, "rev-parse", "main^{tree}")
    layer_oid = subprocess.run(["git", "hash-object", "--stdin"], input=layer, capture_output=True, check=True,
                               cwd=remote).stdout.decode().strip()
    blobs = [line.split()[2] for line in git(remote, "ls-tree", "-r", "main").splitlines() if line.split()[1] == "blob"]
    assert layer_oid in blobs and len(blobs) > 1
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--bare", "--filter=blob:none", remote.as_uri(), str(clone))
    for oid in blobs:
        if oid != layer_oid:
            git(clone, "cat-file", "blob", oid)   # lazily fetched now, before the mark is installed
    assert subprocess.run(["git", "cat-file", "-e", layer_oid], cwd=clone, env={"GIT_NO_LAZY_FETCH": "1", "PATH": "/usr/bin:/bin"}).returncode != 0
    mark = tmp_path / "fetched"
    pack = tmp_path / "upload-pack"
    pack.write_text(f"#!/bin/sh\ntouch {mark}\nexec git-upload-pack \"$@\"\n")
    pack.chmod(0o755)
    git(clone, "config", "remote.origin.uploadpack", str(pack))
    with pytest.raises(mft.MissingLocally) as refused:
        mft.read(clone, tree)
    assert not mark.exists(), "the layer size read tried to fetch"
    assert "git cat-file -s failed" in str(refused.value) and layer_oid in str(refused.value)
