"""Pinned root files arrive together; reading them must not trigger one fetch per blob."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import lane_downstream as ld

from tests.test_lane_downstream import _g


def _remote(tmp_path: Path, *, root_files: bool = True) -> tuple[Path, str]:
    repo = tmp_path / "remote"
    repo.mkdir()
    _g(repo, "init", "-q")
    _g(repo, "config", "user.email", "test@example.com")
    _g(repo, "config", "user.name", "Test")
    (repo / "nested").mkdir()
    (repo / "nested" / "hidden.toml").write_text("nested = true\n")
    if root_files:
        for name in ("a.toml", "same.toml"):
            (repo / name).write_text("same = true\n")
        (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (repo / "run.sh").chmod(0o755)
        (repo / "link.toml").symlink_to("a.toml")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "fixture")
    return repo, _g(repo, "rev-parse", "HEAD")


def _fetch_count(trace: Path) -> int:
    if not trace.exists():
        return 0
    return sum(
        bool(re.search(r"packet:\s+fetch> command=fetch\b", line))
        for line in trace.read_text().splitlines()
    )


def test_root_blobs_are_unique_in_one_fetch_before_any_read(tmp_path: Path, monkeypatch) -> None:
    repo, pin = _remote(tmp_path)
    trace = tmp_path / "packets.log"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(trace))
    calls = []
    read_fetches = []
    real = ld._git_in

    def observed(cwd, *args, **kwargs):
        calls.append(args)
        before = _fetch_count(trace)
        result = real(cwd, *args, **kwargs)
        if args[:2] == ("cat-file", "blob"):
            read_fetches.append(_fetch_count(trace) - before)
        return result

    monkeypatch.setattr(ld, "_git_in", observed)
    dest = tmp_path / "probe"
    probe = ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path / "ws")
    batch = [args for args in calls if args[:3] == ("fetch", "-q", "origin")]
    assert len(batch) == 1
    assert len(batch[0][3:]) == len(set(batch[0][3:])) == 2
    first_read = next(i for i, args in enumerate(calls) if args[:2] == ("cat-file", "blob"))
    assert calls.index(batch[0]) < first_read
    assert _fetch_count(trace) == 2 and read_fetches == [0, 0, 0]
    assert probe.pin == pin and probe.source == "url" and probe.filtered
    assert sorted(p.name for p in dest.iterdir()) == ["a.toml", "run.sh", "same.toml"]
    for path in dest.iterdir():
        assert path.read_bytes() == subprocess.check_output(["git", "-C", str(repo), "show", f"{pin}:{path.name}"])


def test_no_eligible_root_blobs_skips_the_second_fetch(tmp_path: Path, monkeypatch) -> None:
    repo, pin = _remote(tmp_path, root_files=False)
    calls = []
    real = ld._git_in

    def observed(cwd, *args, **kwargs):
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ld, "_git_in", observed)
    dest = tmp_path / "probe"
    ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path / "ws")
    assert len([args for args in calls if args[0] == "fetch"]) == 1
    assert not any(args[:2] == ("cat-file", "blob") for args in calls)
    assert dest.is_dir() and list(dest.iterdir()) == []


@pytest.mark.parametrize("failure", ["exit", "timeout", "os-error"])
def test_failed_batch_refuses_before_copy_or_lazy_read(tmp_path: Path, monkeypatch, failure: str) -> None:
    repo, pin = _remote(tmp_path)
    reads = []
    probe_repos = set()
    real = ld._git_in

    def fail_batch(cwd, *args, **kwargs):
        probe_repos.add(cwd)
        if args[:2] == ("cat-file", "blob"):
            reads.append(args)
        if args[:3] == ("fetch", "-q", "origin"):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 600)
            if failure == "os-error":
                raise OSError("fixture")
            return subprocess.CompletedProcess(args, 1, b"", b"fixture")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ld, "_git_in", fail_batch)
    dest = tmp_path / "probe"
    with pytest.raises(ld.PinRefused, match="root blob"):
        ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path / "ws")
    assert not dest.exists() and reads == []
    assert probe_repos and all(not path.exists() for path in probe_repos)


def test_batch_failure_uses_next_pin_source(tmp_path: Path, monkeypatch) -> None:
    repo, pin = _remote(tmp_path)
    monkeypatch.setattr(ld, "pin_sources", lambda *_: [("cache", str(repo)), ("url", str(repo))])
    real = ld._git_in
    batches = []

    def fail_first(cwd, *args, **kwargs):
        if args[:3] == ("fetch", "-q", "origin"):
            batches.append(args)
            if len(batches) == 1:
                return subprocess.CompletedProcess(args, 1, b"", b"fixture")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ld, "_git_in", fail_first)
    dest = tmp_path / "probe"
    probe = ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path / "ws")
    assert len(batches) == 2 and probe.source == "url" and probe.pin == pin
    assert sorted(p.name for p in dest.iterdir()) == ["a.toml", "run.sh", "same.toml"]
