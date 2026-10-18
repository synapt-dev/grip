"""`review merge` when the push fails: which side said no, and what reaches the receipt.

push_rejected is only a refusal the remote made (porcelain `[remote rejected]`); a push that never reached it (a local
pre-push hook, an auth failure) is push_failed; a push that did not finish (gitops' rc 124 expiry) is push_unconfirmed,
exit 4. The reason is escaped first, then cut to its head with a marker. Every row here runs under a temp path longer
than 150 characters, so git's own "To <url>" lines would push a hook's reason out of any tail-cut field.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from gr2.python_cli import gitops, merge_gate
from tests.test_review_approvals import app, git, runner, set_policy, world  # noqa: F401  (fixtures)
from tests.test_review_merge_gate import slice2  # noqa: F401  (fixture)
from tests.test_review_merge_target_moved import _merge2, _refuse_pushes, _rows, _wrap, mains_at_base

REAL_RUN = subprocess.run


@pytest.fixture
def tmp_path(tmp_path):
    deep = tmp_path / ("long-" + "p" * 80) / ("path-" + "q" * 80)
    deep.mkdir(parents=True)
    assert len(str(deep)) > 150
    return deep


def _merge(world):
    set_policy(world, 0)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    return out, json.loads(out.stdout)["members"][0]


def _on_push(monkeypatch, fake):
    """Every git command runs for real except a push, which `fake(cmd, kw)` answers (or None: run it for real)."""
    def run(cmd, *a, **kw):
        if (isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git" and gitops.command_of(cmd) == "push"
                and str(cmd[-1]).endswith(":refs/heads/main")):  # the member's push, not a check record's
            got = fake(cmd, kw)
            if got is not None:
                return got
        return REAL_RUN(cmd, *a, **kw)
    monkeypatch.setattr(merge_gate.subprocess, "run", run)


def _hook(path, name, body):
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"#!/bin/sh\n" + body)
    (path / name).chmod(0o755)


def test_single_member_push_rejected_keeps_the_hook_reason_under_a_long_path(world, monkeypatch):
    assert len(str(world["remote"])) > 150
    _refuse_pushes(world["remote"], "protected branch")
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["final"] == "unmerged" and row["refused"].startswith("push_rejected: remote: protected branch"), row
    assert str(world["remote"]) not in row["refused"], row
    assert "the remote refused the push" in row["next"], row


def test_a_local_pre_push_hook_reads_push_failed_not_push_rejected(world, monkeypatch):
    hooks = world["remote"].parent / "local-hooks"
    _hook(hooks, "pre-push", b"echo 'local policy: no pushes today' >&2\nexit 1\n")

    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_failed: ") and "local policy: no pushes today" in row["refused"], row
    assert "did not reach the remote" in row["next"] and "clear the rejection" not in row["next"], row
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_an_auth_failure_reads_push_failed(world, monkeypatch):
    _on_push(monkeypatch, lambda cmd, kw: subprocess.CompletedProcess(
        cmd, 128, stdout=b"", stderr=b"fatal: Authentication failed for 'https://example.invalid/r.git/'\n"))
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"] == "push_failed: fatal: Authentication failed for 'https://example.invalid/r.git/'", row
    assert "auth, network, a local hook" in row["next"], row


def test_a_push_timeout_reads_push_unconfirmed_exit_4(world, monkeypatch):
    def fake(cmd, kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_PARTIAL, out.output
    assert row["refused"] == "push_unconfirmed: git timed out after 120s", row
    assert "gr2 review merged" in row["next"] and "not pushed twice" in row["next"], row


def test_a_10kb_hook_message_is_escaped_first_then_cut_with_a_marker(world, monkeypatch):
    _refuse_pushes(world["remote"], "GH006 protected " + "x" * 10_000)
    out, row = _merge(world)
    refused = row["refused"]
    assert refused.startswith("push_rejected: remote: GH006 protected"), refused
    body = refused.removeprefix("push_rejected: ")
    head, _, marker = body.rpartition(" [")
    assert len(head) == 300 and marker.endswith(" chars cut]"), refused
    assert int(marker.split()[0]) > 9_000, refused


def test_invalid_utf8_from_a_rejecting_hook_is_a_refusal_not_a_crash(world, monkeypatch):
    _hook(world["remote"] / "hooks", "pre-receive", (b"while read old new ref; do\n  if [ \"$ref\" = refs/heads/main ]; then\n"
                                                           b"    printf 'bad \\377 byte\\n' >&2; exit 1\n  fi\ndone\nexit 0\n"))
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_rejected: remote: bad \\xff byte"), row


def test_invalid_utf8_from_a_landed_push_reads_merged(world, monkeypatch):
    _hook(world["remote"] / "hooks", "post-receive", b"printf 'done \\377\\n' >&2\nexit 0\n")
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_MERGED, out.output
    assert row["final"] == "merged" and row["refused"] is None, row


def test_push_rejected_keeps_the_head_of_a_long_hook_message(slice2, monkeypatch):
    a = slice2["members"]["alpha"]
    _wrap(monkeypatch, lambda remote: None)
    code, receipt = _merge2(slice2, lambda: _refuse_pushes(a["remote"], "GH006 protected " + "x" * 260))
    refused = _rows(receipt)["alpha"]["refused"]
    assert refused.startswith("push_rejected: remote: GH006 protected"), refused
    assert refused.endswith(" chars cut]"), refused
    assert mains_at_base(slice2)


def test_target_unread_names_the_exception_type_of_a_real_failed_read(slice2, monkeypatch):
    a, b = slice2["members"]["alpha"], slice2["members"]["beta"]
    _wrap(monkeypatch, lambda remote: os.rename(b["remote"], str(b["remote"]) + ".gone")
          if remote == str(b["remote"]) else None)
    code, receipt = _merge2(slice2)
    rows = _rows(receipt)
    assert code == merge_gate.EXIT_PARTIAL and rows["alpha"]["final"] == "merged", receipt
    assert rows["beta"]["refused"].startswith("target_unread_at_push: RuntimeError: git ls-remote"), rows["beta"]


def test_nothing_to_merge_does_not_claim_there_was_no_merge_commit():
    from gr2.python_cli import next_steps
    text = next_steps._nothing_to_merge()
    assert "merge commit" not in text and "already in the target" in text, text
