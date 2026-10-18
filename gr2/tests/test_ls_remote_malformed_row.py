"""A non-empty ls-remote line without a tab is a FAULT, never absence. A git shim answers every ls-remote with
`not-a-valid-ls-remote-row`, alone or beside a good row naming the asked ref, and each caller of
gitops.ls_remote_rows must refuse rather than read a ref as absent or skip the line."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from gr2.python_cli import app, gitops, grip_cli, merge_gate, push

BAD = "not-a-valid-ls-remote-row"
OID = "1" * 40


@pytest.fixture(params=["alone", "beside"])
def shim(request, tmp_path, monkeypatch):
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    good = '' if request.param == "alone" else (
        'last=; for a in "$@"; do last=$a; done\n'
        'case "$*" in *--symref*) printf "ref: refs/heads/main\\tHEAD\\n";; *) printf "%s\\t%s\\n" ' + OID + ' "$last";; esac\n')
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(f'#!/bin/sh\nfor a in "$@"; do [ "$a" = ls-remote ] && {{\n{good}echo {BAD}\nexit 0; }}; done\n'
                              f'exec {real} "$@"\n')
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    repo = tmp_path / "r"
    subprocess.run([real, "init", "-q", str(repo)], check=True)
    subprocess.run([real, "-C", str(repo), "remote", "add", "origin", str(tmp_path / "o.git")], check=True)
    return repo


def test_sink(shim):
    with pytest.raises(ValueError):
        gitops.ls_remote_rows(subprocess.run(["git", "ls-remote", "origin", "refs/heads/x"], cwd=shim,
                                             capture_output=True, text=True).stdout)


CALLS = {
    "push_receipt": lambda r: push._remote_branch_sha(r, "origin", "x"),
    "merge_gate_advertised": lambda r: merge_gate._advertised(r, "origin", "refs/heads/x"),
    "grip_leases": lambda r: grip_cli._leases(r, OID, ["refs/heads/x"]),
    "app_live_remote_tips": lambda r: app._live_remote_tips(r, "origin", "refs/heads/x"),
}


@pytest.mark.parametrize("call", CALLS)
def test_caller_refuses(shim, call, capsys):
    with pytest.raises(BaseException) as got:
        CALLS[call](shim)
    said = str(got.value) + capsys.readouterr().err
    assert "not a row" in said and BAD in said, said


def _commit(repo):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "c"], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_atomic_push_refusal_survives_garbled_listing(shim):
    with pytest.raises(RuntimeError, match="atomic push refused"):
        grip_cli._atomic_push(shim, [(OID, "refs/heads/x")], verify=False)


def test_lane_branch_starts_at_pin(shim, capsys):
    head = _commit(shim)
    assert app._start_lane_branch("r", shim, "lane", None, seed_is_work=False) == (head, "pin")
    err = capsys.readouterr().err
    assert "cannot read the live tip of origin/HEAD; lane starts at the pin" in err, err


def test_pr_create_refuses(shim, capsys, monkeypatch):
    m = {"key": "r", "commit": _commit(shim), "remote": "origin"}
    subprocess.run(["git", "-C", str(shim), "checkout", "-q", "-b", "x"], check=True)
    monkeypatch.setattr(app, "resolve_review_subject", lambda ws, review: ("gr:t", [m]))
    monkeypatch.setattr(app.pr_ops, "review_pr_groups", lambda ws, target: [])
    monkeypatch.setattr(app.platform_ops, "get_platform_adapter", lambda platform: None)
    monkeypatch.setattr(app.platform_ops, "adapter_api_version", lambda adapter: 1)
    monkeypatch.setattr(app, "_review_members_on_host", lambda ws, members, v2: {"o/r": (m, shim)})
    monkeypatch.setattr(app, "_effective_target_matches", lambda v2, ws, remote: True)
    monkeypatch.setattr("gr2.python_cli.review_members.workspace_members", lambda ws: [])
    with pytest.raises(typer.Exit):
        app._pr_create_for_review(shim, None, "github", "main", False, None, None, None)
    err = capsys.readouterr().err
    assert "refused:" in err and "not a row" in err and BAD in err, err
