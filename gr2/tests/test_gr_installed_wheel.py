"""The `gr` a user gets from the built wheel: installed into a fresh environment, run by name from PATH.

The conformance table runs the resolver module through a launcher. This runs what pip actually writes, so a
console-script line that points at the wrong function, or a wheel that leaves the module out, fails here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GR2 = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build and install the wheel"),
    pytest.mark.skipif(os.name == "nt", reason="the stubs are POSIX shell scripts"),
]


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    work = tmp_path_factory.mktemp("wheel")
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(work / "dist"), str(GR2)], check=True)
    wheel = next((work / "dist").glob("gitgrip-*.whl"))
    venv = work / "venv"
    subprocess.run(["uv", "venv", "-q", "-p", sys.executable, str(venv)], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheel)], check=True)
    return venv / "bin"


def _run(bin_dir, cwd, *args, extra_path=()):
    env = {"HOME": str(cwd), "PATH": os.pathsep.join([*map(str, extra_path), str(bin_dir), "/usr/bin", "/bin"])}
    return subprocess.run(["gr", *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_the_wheel_installs_gr_beside_gr2(installed):
    assert (installed / "gr").is_file() and (installed / "gr2").is_file()


def test_outside_any_workspace_gr_is_gr2(installed, tmp_path):
    which = _run(installed, tmp_path, "--which")
    assert which.returncode == 0 and which.stdout.split()[0] == "gr2", which
    assert os.path.realpath(which.stdout.split()[2]) == os.path.realpath(installed / "gr2")
    version = _run(installed, tmp_path, "--version")
    own = subprocess.run([str(installed / "gr2"), "--version"], cwd=tmp_path, capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout == own.stdout, (version, own)


def test_a_gr1_workspace_without_gr1_refuses_and_says_how(installed, tmp_path):
    (tmp_path / ".gitgrip").mkdir()
    result = _run(installed, tmp_path, "status")
    assert result.returncode == 69, result
    assert "gr2 workspace migrate-gr1" in result.stderr and "brew install synapt-dev/tap/gitgrip" in result.stderr


def test_a_gr1_workspace_runs_gr1_installed_as_gitgrip(installed, tmp_path):
    (tmp_path / "ws" / ".gitgrip").mkdir(parents=True)
    brew = tmp_path / "brew"
    brew.mkdir()
    (brew / "gitgrip").write_text('#!/bin/sh\necho "STUB gitgrip $*"\n')
    (brew / "gitgrip").chmod(0o755)
    result = subprocess.run(
        [str(installed / "gr"), "status"], cwd=tmp_path / "ws", capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": os.pathsep.join([str(brew), str(installed), "/usr/bin", "/bin"])},
    )
    assert result.returncode == 0 and result.stdout.startswith("STUB gitgrip status"), result
