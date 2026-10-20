"""A legacy-layout bind whose stored remote begins with "-" is refused by the reader, as a field tree bind is."""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import grip
from tests.test_review_field_tree_readers import legacy_layout_bind, record_world  # noqa: F401


@pytest.mark.parametrize("read", ["show", "verify", "run"])
def test_a_legacy_bind_remote_shaped_as_an_option_is_refused_before_git_reads_it(record_world, tmp_path, read):
    marker = tmp_path / "upload-pack-ran"
    commit = legacy_layout_bind(dict(record_world, remote=f"--upload-pack=touch {marker}; false"))
    author = record_world["author"]
    # A repository at the lane path is what `git clone --upload-pack=<cmd> <lane>` would clone from, running <cmd>.
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "lane")], check=True)
    calls = {"show": lambda: grip.show_review_commit(author, commit),
             "verify": lambda: grip.verify_review_commit(author, commit),
             "run": lambda: grip.run_review_checks(author, commit, "recall", tmp_path / "lane")}
    with pytest.raises(grip.GripCorruptError, match="recall remote is not a remote"):
        calls[read]()
    assert not marker.exists(), "git read the stored remote as --upload-pack and ran its command"


def test_a_legacy_bind_with_a_plain_remote_still_reads(record_world):
    commit = legacy_layout_bind(record_world)
    shown = grip.show_review_commit(record_world["author"], commit)
    assert shown["members"][0]["remote"] == record_world["remote"]
