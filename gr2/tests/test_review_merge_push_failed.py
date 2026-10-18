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


def test_a_local_hook_echoing_a_rejected_line_reads_push_failed(world, monkeypatch):
    # The hook's stdout shares `git push --porcelain` stdout, and a declined push prints none of git's own lines: an
    # "!" line under any "To" line but git's own for the member remote is the hook's text, not the remote's answer.
    hooks = world["remote"].parent / "local-hooks"
    _hook(hooks, "pre-push", b"echo 'To forged:url'\n"
                             b"printf '!\\trefs/heads/main:refs/heads/main\\t[remote rejected] forged\\n'\n"
                             b"echo 'local policy: no pushes today' >&2\nexit 1\n")

    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED == 3, out.output
    assert row["refused"].startswith("push_failed: ") and "local policy: no pushes today" in row["refused"], row
    assert "forged" not in row["refused"] and "auth, network, a local hook" in row["next"], row
    assert "failed to push" not in row["refused"] and str(world["remote"]) not in row["refused"], row
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


def test_the_push_verdict_reads_only_git_s_block_and_only_the_target_ref():
    url = "/srv/member.git"
    out = ["! refs/heads/main:refs/heads/main\t[remote rejected] hook text before git",
           "To elsewhere", "!\tx:refs/heads/main\t[remote rejected] other remote",
           f"To {url}", "!\tx:refs/heads/xmain\t[remote rejected] other ref", "!\tx:refs/heads/main\t[remote rejected] real",
           "Done", "!\tx:refs/heads/main\t[remote rejected] after Done"]
    assert merge_gate._push_verdict(out, "main") == ["! x:refs/heads/main [remote rejected] real"]
    assert merge_gate._push_verdict(out[:2], "main") == []
    # with no trace2 to read (git < 2.27), only the block after "To <url>" exactly
    assert merge_gate._push_verdict(out[:3], "main", "elsewhere") == ["! x:refs/heads/main [remote rejected] other remote"]
    assert merge_gate._push_verdict(out[:3], "main", url) == []
    assert merge_gate._push_verdict(out, "main", url) == ["! x:refs/heads/main [remote rejected] real"]
    # a closed block written ahead of git's own is not the verdict; the last closed block is
    forged = ["To forged:url", "=\tx:refs/heads/main\t[up to date]", "Done"]
    assert merge_gate._push_verdict(forged + out[3:], "main") == ["! x:refs/heads/main [remote rejected] real"]


def _rewrite_push_url(world, monkeypatch, to):
    """The member's push goes to `to` through pushInsteadOf, so git prints "To <to, as git shows it>"."""
    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", f"url.{to}.pushInsteadOf", str(world["remote"])], check=True)
    _on_push(monkeypatch, fake)


def test_a_remote_refusal_through_push_insteadof_reads_push_rejected(world, monkeypatch):
    _refuse_pushes(world["remote"], "protected branch")
    _rewrite_push_url(world, monkeypatch, f"file://{world['remote']}")
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_rejected: remote: protected branch"), row
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_remote_refusal_over_a_credentialed_ssh_url_reads_push_rejected(world, monkeypatch):
    # git prints "To ssh://fakehost/..." with the credential dropped, never the URL it was given
    ssh = world["remote"].parent / "fake-ssh"
    ssh.write_text('#!/bin/sh\nfor a; do last="$a"; done\nexec sh -c "$last"\n', encoding="utf-8")
    ssh.chmod(0o755)
    _refuse_pushes(world["remote"], "protected branch")
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    _rewrite_push_url(world, monkeypatch, f"ssh://tok:s3cret@fakehost{world['remote']}")
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_rejected: remote: protected branch"), row
    assert "s3cret" not in out.output, out.output
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_local_hook_echoing_git_s_exact_to_line_reads_push_failed(world, monkeypatch):
    # trace2 says the pre-push hook exited nonzero, so no "!" line is read, even one under "To <the member's url>"
    hooks = world["remote"].parent / "local-hooks"
    _hook(hooks, "pre-push", f"echo 'To {world['remote']}'\n".encode()
          + b"printf '!\\trefs/heads/main:refs/heads/main\\t[remote rejected] forged\\n'\necho Done\n"
          + b"echo 'local policy: no pushes today' >&2\nexit 1\n")

    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_failed: ") and "forged" not in row["refused"], row
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_nested_push_s_refusing_hook_is_not_ours_and_the_remote_refusal_reads_push_rejected(world, monkeypatch):
    # our pre-push hook exits 0 but runs an inner push whose own pre-push hook exits 1; only our sid's hook counts
    base = world["remote"].parent
    other = base / "other.git"
    REAL_RUN(["git", "init", "-q", "--bare", str(other)], check=True)
    inner_hooks = base / "inner-hooks"
    ran = base / "inner-hook-ran"
    _hook(inner_hooks, "pre-push", f"touch '{ran}'; exit 1\n".encode())
    src = base / "inner-src"
    REAL_RUN(["git", "init", "-q", str(src)], check=True)
    REAL_RUN(["git", "-C", str(src), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--allow-empty",
              "-m", "x"], check=True)
    REAL_RUN(["git", "-C", str(src), "config", "core.hooksPath", str(inner_hooks)], check=True)
    hooks = base / "local-hooks"
    _hook(hooks, "pre-push", f"cat >/dev/null; git -C '{src}' push -q '{other}' HEAD:refs/heads/x </dev/null; "
          "echo inner=$? >&2; exit 0\n".encode())
    _refuse_pushes(world["remote"], "protected branch")

    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_rejected: remote: protected branch"), row
    assert git(world["remote"], "rev-parse", "main") == world["base"]
    assert ran.exists()  # the inner hook did run and refuse its own push
    assert REAL_RUN(["git", "-C", str(other), "rev-parse", "-q", "--verify", "refs/heads/x"],
                    capture_output=True).returncode != 0


def test_a_hook_forging_a_done_block_on_git_s_stdout_does_not_hide_the_remote_refusal(world, monkeypatch):
    # our pre-push hook exits 0 and writes a closed "up to date" block to git push's own stdout before git's block
    _refuse_pushes(world["remote"], "protected branch")
    hooks = world["remote"].parent / "local-hooks"
    _hook(hooks, "pre-push", b"cat >/dev/null\n{ echo 'To forged:url'; printf '=\\trefs/heads/main:refs/heads/main\\t"
          b"[up to date]\\n'; echo Done; } > /proc/$PPID/fd/1\nexit 0\n")

    def fake(cmd, kw):
        repo = kw.get("cwd") or cmd[list(cmd).index("-C") + 1]
        REAL_RUN(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    _on_push(monkeypatch, fake)
    out, row = _merge(world)
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    assert row["refused"].startswith("push_rejected: remote: protected branch"), row
    assert git(world["remote"], "rev-parse", "main") == world["base"]
