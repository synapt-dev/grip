"""A full-mode member's record read stays local. In a partial clone the record's trees or blobs may be absent; the
reader refuses such an object, naming the fetch that brings it in, and nothing reaches the promisor remote."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gr2.python_cli import member_field_tree as mft
from tests.test_review_transport import git


@pytest.mark.parametrize("shape", ["blob:none", "tree:0"])
def test_member_record_read_refuses_an_absent_object_without_fetching(tmp_path, shape):
    """The remote stays in place, so a lazy fetch would succeed; the read refuses instead, and a plain cat-file of the
    same tree, which does fetch, shows the mark is live."""
    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    record = dict(kind=mft.KIND_REPO, pin="a" * 40, url="https://example.com/x.git", ref="main",
                  layers=[b"# v2 git bundle\nlayer\n"])
    git(remote, "update-ref", "refs/heads/main", mft.commit(remote, record, "record"))
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    tree = git(remote, "rev-parse", "main^{tree}")
    schema = git(remote, "rev-parse", "main:001.2_schema")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--bare", f"--filter={shape}", remote.as_uri(), str(clone))
    # a fetch from the remote runs this upload-pack, which leaves a mark and then serves the objects
    mark = tmp_path / "fetched"
    pack = tmp_path / "upload-pack"
    pack.write_text(f"#!/bin/sh\ntouch {mark}\nexec git-upload-pack \"$@\"\n")
    pack.chmod(0o755)
    git(clone, "config", "remote.origin.uploadpack", str(pack))
    with pytest.raises(Exception) as refused:
        mft.read(clone, tree)
    assert not mark.exists(), "the record read tried to fetch"
    assert refused.type is mft.MissingLocally   # a FieldTreeError a caller can tell from "not a record"
    assert "is missing locally (partial clone?); bring it in with: git -C" in str(refused.value)
    assert " fetch -- origin " in str(refused.value)
    assert git(clone, "cat-file", "blob", schema) == mft.PACKAGE  # without the variable the same read fetches
    assert mark.exists()


def test_member_record_read_in_a_full_clone_is_whole(tmp_path):
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    record = dict(kind=mft.KIND_REPO, pin="a" * 40, url="https://example.com/x.git", layers=[b"# v2 git bundle\nl\n"])
    tree = git(remote, "rev-parse", mft.commit(remote, record, "record") + "^{tree}")
    got = mft.read(remote, tree)
    assert (got["kind"], got["pin"], got["layers"]) == (mft.KIND_REPO, "a" * 40, [b"# v2 git bundle\nl\n"])


def test_a_grip_command_in_a_blob_none_clone_prints_the_refusal_and_exits_non_zero(tmp_path):
    """The command's record read meets a blob the clone does not hold: it is refused by name, not a traceback."""
    from tests.test_store_full_mode_carrier import _full_store, _legacy_root, gr2

    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    root = _legacy_root(_full_store(tmp_path))
    git(root, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    git(tmp_path, "clone", "-q", "--filter=blob:none", root.as_uri(), str(clone))

    made = gr2(clone, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert "Traceback" not in out, out
    assert made.returncode != 0, out
    assert "field record cannot be read" in out and "is missing locally (partial clone?)" in out, out


def test_the_partial_clone_refusal_does_not_tell_the_user_to_unshallow(tmp_path):
    """A blob:none clone is not shallow: its refusal names the fetch, and the unshallow advice would mislead."""
    from tests.test_store_full_mode_carrier import _full_store, _legacy_root, gr2

    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    root = _legacy_root(_full_store(tmp_path))
    git(root, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    git(tmp_path, "clone", "-q", "--filter=blob:none", root.as_uri(), str(clone))

    made = gr2(clone, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode != 0 and "is missing locally (partial clone?)" in out, out
    assert "--unshallow" not in out, out


def test_a_shallow_clone_without_the_record_still_names_unshallow(tmp_path):
    """The record is a parent of the grip commit, so a depth-1 clone does not reach it: that refusal keeps the advice."""
    from tests.test_store_full_mode_carrier import _full_store, _legacy_root, gr2

    root = _legacy_root(_full_store(tmp_path))
    clone = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth=1", root.as_uri(), str(clone))
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"

    made = gr2(clone, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert "Traceback" not in out, out
    assert made.returncode != 0 and "field record cannot be read" in out, out
    assert "is missing locally" not in out, out
    assert "git fetch --unshallow" in out, out
