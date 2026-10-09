"""The shadow line: once per install, only for a gr1 without the resolver, never under --json or quiet."""

from __future__ import annotations

import os
import time

import pytest

from gr2.python_cli import gr_shadow
from gr2.python_cli.gr_shadow import MARKER, shadow_line

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stubs are POSIX shell scripts")


def _stub(directory, name, text, delay=0):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    sleep = f"sleep {delay}\n" if delay else ""
    path.write_text(f"#!/bin/sh\n{sleep}echo '{text}'\n")
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))  # never the real home
    monkeypatch.delenv("GR2_QUIET_CONTEXT", raising=False)
    return prefix, gr2, brew


def test_a_gr1_without_the_resolver_first_on_path_is_named_once(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    first = shadow_line([], str(gr2), str(prefix))
    assert first and "gitgrip 1.5.2" in first and "Type `gr2`" in first and "(shown once)" in first
    assert (prefix / MARKER).read_text().startswith(os.path.realpath(brew / "gr") + " ")
    assert shadow_line([], str(gr2), str(prefix)) is None, "the line printed a second time"


def test_a_resolver_gr_first_on_path_is_not_a_shadow(install):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.6.0")
    assert shadow_line([], str(gr2), str(prefix)) is None
    assert (prefix / MARKER).exists(), "a judged gr is recorded, so it is not judged on every run"


def test_json_and_quiet_never_carry_it_and_record_nothing(install, monkeypatch):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    assert shadow_line(["status", "--json"], str(gr2), str(prefix)) is None
    monkeypatch.setenv("GR2_QUIET_CONTEXT", "1")
    assert shadow_line(["status"], str(gr2), str(prefix)) is None
    assert not (prefix / MARKER).exists()
    monkeypatch.delenv("GR2_QUIET_CONTEXT")
    assert shadow_line(["status"], str(gr2), str(prefix)) is not None, "a silenced run used up the one showing"


def test_ours_first_on_path_says_nothing(install, monkeypatch):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    monkeypatch.setenv("PATH", os.pathsep.join([str(prefix / "bin"), str(brew), "/usr/bin", "/bin"]))
    assert shadow_line([], str(gr2), str(prefix)) is None


def test_an_unwritable_prefix_records_in_the_user_state_and_still_shows_once(install, tmp_path):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    prefix.chmod(0o555)
    try:
        assert shadow_line([], str(gr2), str(prefix)) is not None, "a pip --user install was never told"
        assert not (prefix / MARKER).exists()
        assert gr_shadow._user_state(str(prefix)).is_file()
        assert shadow_line([], str(gr2), str(prefix)) is None, "the line printed a second time"
    finally:
        prefix.chmod(0o755)


def test_nothing_writable_prints_nothing_rather_than_every_run(install, tmp_path, monkeypatch):
    prefix, gr2, brew = install
    _stub(brew, "gr", "gr 1.5.2")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    state = tmp_path / "ro-state"
    state.mkdir()
    state.chmod(0o555)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    prefix.chmod(0o555)
    try:
        assert shadow_line([], str(gr2), str(prefix)) is None
    finally:
        prefix.chmod(0o755)
        state.chmod(0o755)


def test_a_timeout_is_not_a_verdict_and_is_judged_again(install, monkeypatch):
    prefix, gr2, brew = install
    gr = _stub(brew, "gr", "gr 1.5.2")
    monkeypatch.setattr(gr_shadow, "_gr1_version", lambda g: gr_shadow._TIMEOUT)
    assert shadow_line([], str(gr2), str(prefix)) is None
    assert not (prefix / MARKER).exists(), "a timeout was recorded as a verdict"
    monkeypatch.undo()  # restores the caller's environment too, so set what this test needs again
    monkeypatch.setenv("PATH", os.pathsep.join([str(brew), str(prefix / "bin"), "/usr/bin", "/bin"]))
    monkeypatch.setenv("XDG_STATE_HOME", str(prefix.parent / "state"))
    monkeypatch.delenv("GR2_QUIET_CONTEXT", raising=False)
    assert gr.exists() and shadow_line([], str(gr2), str(prefix)) is not None


def test_a_gr_replaced_in_place_is_judged_again(install):
    prefix, gr2, brew = install
    gr = _stub(brew, "gr", "gr 1.6.0")
    assert shadow_line([], str(gr2), str(prefix)) is None
    time.sleep(0.01)
    _stub(brew, "gr", "gr 1.5.1")  # same path, as `cargo install` overwrites it
    os.utime(gr, ns=(time.time_ns(), time.time_ns()))
    assert shadow_line([], str(gr2), str(prefix)) is not None
