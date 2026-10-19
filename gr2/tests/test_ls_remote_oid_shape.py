"""An ls-remote row whose oid is not 40 (sha1) or 64 (sha256) lowercase hex is a FAULT, never a tip. A git shim answers
every ls-remote with `<oid>\t<asked ref>` for a bad oid (not hex, short, long, uppercase, or a "ref: " symref row
from a listing that never asked --symref), and gitops.ls_remote_rows, gitops.ls_remote_oid and each real caller must
refuse; a 64-hex oid from a sha256 remote reads as a tip."""
from __future__ import annotations

import os
import subprocess

import pytest

from gr2.prototypes import lane_workspace_prototype as lwp
from gr2.python_cli import app, defaults, gitops, grip_cli, merge_gate, push, review, review_rebind

SHA1 = "1" * 40
SHA256 = "ab" * 32
BAD = {
    "garbage": "garbage",
    "short": "1234abc",
    "long": "1" * 41,
    "upper": "A" * 40,
    "symref_row": "ref: refs/heads/main",
}


def _shim(tmp_path, monkeypatch, oid):
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(
        '#!/bin/sh\nfor a in "$@"; do [ "$a" = ls-remote ] && {\n'
        'last=; for b in "$@"; do last=$b; done\n'
        'case "$*" in *--symref*) printf "ref: refs/heads/main\\tHEAD\\n";; esac\n'
        f'printf "%s\\t%s\\n" "{oid}" "$last"\nexit 0; }}; done\n'
        f'exec {real} "$@"\n')
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    repo = tmp_path / "r"
    subprocess.run([real, "init", "-q", str(repo)], check=True)
    subprocess.run([real, "-C", str(repo), "remote", "add", "origin", str(tmp_path / "o.git")], check=True)
    return repo


@pytest.mark.parametrize("kind", BAD)
def test_rows_refuse(kind):
    with pytest.raises(gitops.LsRemoteMalformed, match="not an object id"):
        gitops.ls_remote_rows(f"{SHA1}\trefs/heads/a\n{BAD[kind]}\trefs/heads/x\n")
    with pytest.raises(gitops.LsRemoteMalformed, match="not an object id"):
        gitops.ls_remote_oid(f"{BAD[kind]}\trefs/heads/x\n", "refs/heads/x")


@pytest.mark.parametrize("oid", [SHA1, SHA256])
def test_rows_accept(oid):
    assert gitops.ls_remote_rows(f"{oid}\trefs/heads/x\n") == [(oid, "refs/heads/x")]
    assert gitops.ls_remote_oid(f"{oid}\trefs/heads/x\n", "refs/heads/x") == oid


def test_symref_row_only_when_asked():
    listing = f"ref: refs/heads/main\tHEAD\n{SHA1}\tHEAD\n"
    assert gitops.ls_remote_rows(listing, symref=True)[0] == ("ref: refs/heads/main", "HEAD")
    with pytest.raises(gitops.LsRemoteMalformed):
        gitops.ls_remote_rows(listing)
    with pytest.raises(gitops.LsRemoteMalformed):
        gitops.ls_remote_rows(f"garbage\tHEAD\n", symref=True)


CALLS = {
    "push_receipt": lambda r: push._remote_branch_sha(r, "origin", "x"),
    "merge_gate_advertised": lambda r: merge_gate._advertised(r, "origin", "refs/heads/x"),
    "grip_leases": lambda r: grip_cli._leases(r, SHA1, ["refs/heads/x"]),
    "app_live_remote_tips": lambda r: app._live_remote_tips(r, "origin", "refs/heads/x"),
    "app_remote_tag_shas": lambda r: app._remote_tag_shas(r),
    "defaults_target_branch": lambda r: defaults.target_branch([(r, "origin", SHA1)], None),
    "review_rebind_remote_oid": lambda r: review_rebind._remote_oid(r, "origin", "refs/heads/x"),
}
# The --symref callers read a "ref: " row by design; every other bad oid they still refuse.
SYMREF_CALLERS = {"defaults_target_branch"}


@pytest.mark.parametrize("kind", BAD)
@pytest.mark.parametrize("call", CALLS)
def test_caller_refuses(tmp_path, monkeypatch, capsys, call, kind):
    if kind == "symref_row" and call in SYMREF_CALLERS:
        pytest.skip("a --symref listing holds a ref: row by design")
    repo = _shim(tmp_path, monkeypatch, BAD[kind])
    with pytest.raises(BaseException) as got:
        CALLS[call](repo)
    said = str(got.value) + capsys.readouterr().err
    assert "not an object id" in said and BAD[kind] in said, said


@pytest.mark.parametrize("kind", [k for k in BAD if k != "symref_row"])
def test_lane_guard_cannot_tell(tmp_path, monkeypatch, kind):
    repo = _shim(tmp_path, monkeypatch, BAD[kind])
    assert lwp._origin_default_branch(repo, "origin") is None


def test_lane_guard_reads_symref(tmp_path, monkeypatch):
    repo = _shim(tmp_path, monkeypatch, SHA1)
    assert lwp._origin_default_branch(repo, "origin") == "main"


def test_sha256_tip_through_callers(tmp_path, monkeypatch):
    repo = _shim(tmp_path, monkeypatch, SHA256)
    assert push._remote_branch_sha(repo, "origin", "x") == SHA256
    assert review_rebind._remote_oid(repo, "origin", "refs/heads/x") == SHA256
    assert merge_gate._advertised(repo, "origin", "refs/heads/x") == SHA256
    assert app._live_remote_tips(repo, "origin", "refs/heads/x") == {"refs/heads/x": SHA256}


@pytest.mark.parametrize("kind", BAD)
def test_review_pr_head_refuses(tmp_path, monkeypatch, kind):
    repo = _shim(tmp_path, monkeypatch, BAD[kind])
    with pytest.raises(review.ReviewError, match="not an object id"):
        review.host_pr_head_oid(repo, 7)


def test_review_pr_head_sha256_refused(tmp_path, monkeypatch):
    # The PR-head reader checks for a 40-hex oid after the read, so a sha256 PR head is refused, not misread.
    repo = _shim(tmp_path, monkeypatch, SHA256)
    with pytest.raises(review.ReviewError, match="non-sha OID"):
        review.host_pr_head_oid(repo, 7)
