"""The shadow line: once per install, only for a gr1 without the resolver, never under --json."""

from __future__ import annotations

import os
import sys

import pytest

from gr2.python_cli.gr_shadow import MARKER, shadow_line

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stubs are POSIX shell scripts")


def _stub(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{text}'\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install prefix holding our `gr2` and `gr`, and a brew-shaped `gr` earlier on PATH."""
    prefix = tmp_path / "venv"
    ours = prefix / "bin"
    gr2 = _stub(ours, "gr2", "gr2")
    _stub(ours, "gr", "ours")
    brew = tmp_path / "brew"
    monkeypatch.setenv("PATH", os.pathsep.join([str(brew), str(ours), "/usr/bin", "/bin"]))
    return prefix, gr2, brew


def test_a_gr1_without_the_resolver_first_on_path_is_named_once(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    first = shadow_line([], str(gr2), str(prefix))
    assert first and "gitgrip 1.5.2" in first and "Type `gr2`" in first and "(shown once)" in first
    assert (prefix / MARKER).read_text() == os.path.realpath(brew / "gr")
    assert shadow_line([], str(gr2), str(prefix)) is None, "the line printed a second time"


def test_a_resolver_gr_first_on_path_is_not_a_shadow(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.6.0")
    assert shadow_line([], str(gr2), str(prefix)) is None
    assert (prefix / MARKER).exists(), "a judged gr is recorded, so it is not judged on every run"


def test_json_output_never_carries_it_and_records_nothing(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    assert shadow_line(["status", "--json"], str(gr2), str(prefix)) is None
    assert not (prefix / MARKER).exists()
    assert shadow_line(["status"], str(gr2), str(prefix)) is not None, "the --json run used up the one showing"


def test_ours_first_on_path_says_nothing(install, monkeypatch):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    monkeypatch.setenv("PATH", os.pathsep.join([str(prefix / "bin"), str(brew), "/usr/bin", "/bin"]))
    assert shadow_line([], str(gr2), str(prefix)) is None


def test_an_unwritable_prefix_prints_nothing_rather_than_every_run(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    prefix.chmod(0o555)
    try:
        assert shadow_line([], str(gr2), str(prefix)) is None
    finally:
        prefix.chmod(0o755)


def test_a_different_gr_appearing_later_is_judged_once_too(install, tmp_path):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.6.0")
    assert shadow_line([], str(gr2), str(prefix)) is None
    _stub(brew, "gr", "gr 1.5.1")  # same path, so it was already judged
    assert shadow_line([], str(gr2), str(prefix)) is None
    other = tmp_path / "other"
    _stub(other, "gr", "gr 1.5.1")
    os.environ["PATH"] = os.pathsep.join([str(other), os.environ["PATH"]])
    assert shadow_line([], str(gr2), str(prefix)) is not None
