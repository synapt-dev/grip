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


def test_form_line_with_member():
    assert grip_cli.host_size_line("web/assets/demo.mp4", "web", "142.30 MB", "100.00 MB") == (
        "push refused by the host for size: web/assets/demo.mp4 in member web is 142.30 MB "
        "(host limit 100.00 MB); nothing moved")
