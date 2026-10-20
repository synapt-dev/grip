"""A ref name may hold U+0085 (git accepts it), which str.splitlines cuts at. The rejected-ref lines of
`git push --porcelain` and the `ls-remote --symref HEAD` row are read by "\n" only, so the atomic push refusal names
the whole ref and the transport probe reads the whole HEAD target."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import repo_transport_probe
from gr2.python_cli import grip_cli

REF = "refs/heads/a\x85b"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path) -> str:
    _git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    return _git("-C", str(repo), "rev-parse", "HEAD")


def test_atomic_push_refusal_names_a_ref_holding_u0085(tmp_path):
    origin, repo = tmp_path / "o.git", tmp_path / "r"
    _git("init", "-q", "--bare", str(origin))
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "remote", "add", "origin", str(origin))
    held = _commit(repo)
    _git("-C", str(repo), "push", "-q", "origin", f"{held}:{REF}")
    moved = _commit(repo)
    with pytest.raises(RuntimeError) as refused:
        grip_cli._atomic_push(repo, [(moved, REF)], verify=False, leases={REF: ""})
    assert f"atomic push refused, no ref moved on origin: {REF} [rejected]" in str(refused.value)


def test_transport_probe_reads_a_head_target_holding_u0085(tmp_path):
    origin, repo = tmp_path / "o.git", tmp_path / "r"
    _git("init", "-q", "--bare", str(origin))
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "push", "-q", str(origin), f"{_commit(repo)}:{REF}")
    _git("-C", str(origin), "symbolic-ref", "HEAD", REF)
    assert repo_transport_probe.probe_url(str(origin)) == ("ok", "a\x85b")
