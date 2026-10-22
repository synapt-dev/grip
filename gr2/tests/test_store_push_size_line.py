"""The store push's refusal: a size refusal leads with gr2's line, then git's stderr as git wrote it."""
import subprocess

import pytest

from gr2.python_cli import grip_cli

SIZE_ERR = (
    "remote: error: Trace: 0123abcd\n"
    "remote: error: See https://gh.io/lfs for more information.\n"
    "remote: error: File web/assets/demo.mp4 is 142.30 MB; this exceeds GitHub's file size limit of 100.00 MB\n"
    "remote: error: GH001: Large files detected. You may want to try Git Large File Storage.\n"
    "To origin\n"
    " ! [remote rejected] abc -> main (pre-receive hook declined)\n"
    "error: failed to push some refs to 'origin'\n"
)
OTHER_ERR = "remote: error: protected branch\nerror: failed to push some refs to 'origin'\n"


def _refuse(monkeypatch, stderr):
    def fake(root, *args, check=True):
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 1, "!\tabc:refs/heads/main\t[remote rejected]\n", stderr)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(grip_cli, "_store_git", fake)
    with pytest.raises(RuntimeError) as err:
        grip_cli._atomic_push(None, [("abc", "refs/heads/main")])
    return str(err.value)


def test_size_refusal_leads_with_gr2_line_then_git_stderr(monkeypatch):
    first, rest = _refuse(monkeypatch, SIZE_ERR).split("\n", 1)
    assert first == ("push refused by the host for size: web/assets/demo.mp4 in a member not determined "
                     "is 142.30 MB (host limit 100.00 MB); nothing moved")
    assert rest == SIZE_ERR


def test_other_refusal_unchanged(monkeypatch):
    got = _refuse(monkeypatch, OTHER_ERR)
    assert got == ("atomic push refused, no ref moved on origin: refs/heads/main [remote rejected]"
                   " (remote: error: protected branch)")


HOOK_LINE = "error: File assets/big.bin is 120.00 MB; this exceeds GitHub's file size limit of 100.00 MB"
# The smallest blob the reader calls over a 100.00 MB limit: one byte past 100 MiB. Zeros pack small.
BIG = 100 * 1024 * 1024 + 1


def _real_refusal(tmp_path, big: int) -> str:
    from tests.test_store_git_native_smoke import git
    from tests.test_store_push_member_refs import _same_repo_store
    root, origin = _same_repo_store(tmp_path, "light")
    gamma = root / "gamma"
    (gamma / "assets").mkdir()
    with open(gamma / "assets" / "big.bin", "wb") as fh:
        fh.truncate(big)
    git(gamma, "add", "assets/big.bin")
    git(gamma, "commit", "-q", "-m", "big")
    git(gamma, "push", "-q", "origin", "HEAD:refs/heads/wip")
    from tests.test_store_git_native_smoke import gr2
    gr2(root, "commit", "-m", "pin big")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho \"{HOOK_LINE}\" >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(Exception) as err:
        grip_cli._native_store_push(root, atomic=True)
    return str(err.value)


def test_a_store_push_names_the_member_whose_pin_carries_the_blob(tmp_path):
    first, rest = _real_refusal(tmp_path, BIG).split("\n", 1)
    assert first == ("push refused by the host for size: assets/big.bin in member gamma is 120.00 MB "
                     "(host limit 100.00 MB); nothing moved")
    assert "remote: " + HOOK_LINE in rest


def test_b_store_push_member_not_determined_when_no_blob_is_over(tmp_path):
    first, rest = _real_refusal(tmp_path, 10).split("\n", 1)
    assert first == ("push refused by the host for size: assets/big.bin in a member not determined is 120.00 MB "
                     "(host limit 100.00 MB); nothing moved")
    assert "remote: " + HOOK_LINE in rest


def test_form_line_with_member():
    assert grip_cli.host_size_line("web/assets/demo.mp4", "web", "142.30 MB", "100.00 MB") == (
        "push refused by the host for size: web/assets/demo.mp4 in member web is 142.30 MB "
        "(host limit 100.00 MB); nothing moved")
