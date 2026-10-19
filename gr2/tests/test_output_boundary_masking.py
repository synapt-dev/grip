"""A message gr2 builds from git's output itself (not through GitError) shows no credential: an output boundary masks
the same way GitError does, through gitops.shown_git_text.
Rows:
O1 a --local-source given as an https URL with a token in its userinfo: the mirror top-up error names the source
   with the userinfo stripped, and no token;
O2 an scp-form source with a token in its query: the error names it by the placeholder, and no token;
O3 a plain local path that does not exist is still named in full (nothing is over-masked);
O4 shown_git_text masks a token git's output echoes, in a URL and bare;
G1 an ls-remote refusal (grip._remote_ref_matches, _head_present_on_remote, _remote_ref_tips) for an https remote with
   a token in its userinfo carries the token in neither its subject nor its detail;
G2 a token that straddles the 120-character cut leaves no prefix in the detail (masking runs before the cut);
G3 a plain remote name and git's plain stderr are shown unchanged.
C1 a failed clone (run_argv stubbed, rc 128) comes back with git's echoed token masked, returncode and args unchanged;
C2 a real failed clone of a URL with a query token shows no token in its stderr;
C3 a clone that exits 0 comes back byte-identical;
C4 a binary clone comes back untouched, even when it fails;
C5 clone_repo's SystemExit shows no token in the stderr part of its message.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops, open_gr_review, project_review

ENV = dict(os.environ, GIT_AUTHOR_NAME="mask-test", GIT_AUTHOR_EMAIL="mask-test@example.invalid",
           GIT_COMMITTER_NAME="mask-test", GIT_COMMITTER_EMAIL="mask-test@example.invalid",
           GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="false")
TOKEN = "FAKEtok3n9z"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env=ENV).stdout.decode().strip()


@pytest.fixture
def world(tmp_path):
    """`origin` (bare) holds `base`; `head` exists only in the clone `me`, so a top-up from a local source runs."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    me = tmp_path / "me"
    subprocess.run(["git", "init", "-q", str(me)], check=True)
    git(me, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(me, "rev-parse", "HEAD")
    git(origin, "fetch", "-q", str(me), "HEAD:refs/heads/main")
    git(me, "commit", "-q", "--allow-empty", "-m", "head")
    return origin, base, git(me, "rev-parse", "HEAD")


def top_up_error(tmp_path, world, local: str) -> str:
    origin, base, head = world
    pin = project_review.ProjectReviewPin(key="a", repo=f"local:{origin}", path="repos/a", base=base, head=head)
    with pytest.raises(open_gr_review.OpenGrReviewError) as caught:
        open_gr_review.resolve_sources_from_pins([pin], cache_root=tmp_path / "cache", local_sources={"a": local})
    return str(caught.value)


def test_o1_an_https_source_is_named_without_its_userinfo(tmp_path, world):
    text = top_up_error(tmp_path, world, f"https://u:{TOKEN}@127.0.0.1:9/x.git")
    assert TOKEN not in text
    assert "local source 'https://127.0.0.1:9/x.git'" in text


def test_o2_an_scp_source_with_a_query_token_is_named_by_the_placeholder(tmp_path, world):
    text = top_up_error(tmp_path, world, f"u@127.0.0.1:x.git?access_token={TOKEN}")
    assert TOKEN not in text
    assert f"local source {gitops._CREDENTIAL_SHOWN!r}" in text


def test_o3_a_missing_local_path_is_still_named(tmp_path, world):
    missing = tmp_path / "no such clone"
    text = top_up_error(tmp_path, world, str(missing))
    assert str(missing) in text
    assert gitops._CREDENTIAL_SHOWN not in text


def test_o4_shown_git_text_masks_an_echoed_token():
    url = f"https://127.0.0.1:9/x.git?access_token={TOKEN}"
    text = gitops.shown_git_text(("fetch", "--", url), f"fatal: unable to access '{url}': the server said {TOKEN}")
    assert TOKEN not in text
    assert gitops._CREDENTIAL_SHOWN in text


LS_REMOTE_SITES = [
    ("_remote_ref_matches", lambda grip, ws, remote: grip._remote_ref_matches(ws, remote, "refs/heads/main")),
    ("_head_present_on_remote", lambda grip, ws, remote: grip._head_present_on_remote(ws, remote, "0" * 40)),
    ("_remote_ref_tips", lambda grip, ws, remote: grip._remote_ref_tips(ws, remote)),
]


def ls_remote_refusal(monkeypatch, tmp_path, call, remote: str, stderr: str):
    from gr2.python_cli import grip

    def failed(workspace, *args, **kwargs):
        return subprocess.CompletedProcess(["git", *args], 128, "", stderr)

    monkeypatch.setattr(grip, "_bind_git", failed)
    with pytest.raises(grip.GripReviewRefused) as caught:
        call(grip, tmp_path, remote)
    assert caught.value.refusal == "remote_unreadable"
    return caught.value


@pytest.mark.parametrize("name,call", LS_REMOTE_SITES, ids=[n for n, _ in LS_REMOTE_SITES])
def test_g1_an_ls_remote_refusal_shows_no_token(monkeypatch, tmp_path, name, call):
    remote = f"https://u:{TOKEN}@h/x.git"
    refused = ls_remote_refusal(monkeypatch, tmp_path, call, remote, f"fatal: unable to access '{remote}/': 403")
    assert TOKEN not in refused.expected
    assert TOKEN not in refused.observed


@pytest.mark.parametrize("name,call", LS_REMOTE_SITES, ids=[n for n, _ in LS_REMOTE_SITES])
def test_g2_a_token_straddling_the_cut_leaves_no_prefix(monkeypatch, tmp_path, name, call):
    remote = f"https://u:{TOKEN}@h/x.git"
    stderr = "fatal: " + "x" * 103 + TOKEN + " was refused"
    assert stderr.index(TOKEN) == 110
    refused = ls_remote_refusal(monkeypatch, tmp_path, call, remote, stderr)
    assert not any(TOKEN[:n] in refused.observed for n in range(4, len(TOKEN) + 1))


@pytest.mark.parametrize("name,call", LS_REMOTE_SITES, ids=[n for n, _ in LS_REMOTE_SITES])
def test_g3_a_plain_remote_name_and_stderr_are_shown_unchanged(monkeypatch, tmp_path, name, call):
    stderr = "fatal: could not read from remote repository"
    refused = ls_remote_refusal(monkeypatch, tmp_path, call, "origin", stderr)
    assert refused.observed == stderr
    assert refused.expected == ("refs/heads/main" if name == "_remote_ref_matches" else "origin")


CLONE_URL = f"https://u:{TOKEN}@h/x.git"
REFUSED_URL = f"https://127.0.0.1:9/x.git?access_token={TOKEN}"


def stub_clone(monkeypatch, returncode: int, stderr):
    calls = []

    def ran(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, returncode, b"" if kwargs.get("binary") else "", stderr)

    monkeypatch.setattr(gitops, "run_argv", ran)
    return calls


def test_c1_a_failed_clone_masks_the_echoed_token(monkeypatch, tmp_path):
    stub_clone(monkeypatch, 128, f"Cloning into 'x'...\nfatal: unable to access '{CLONE_URL}/': 403\n")
    proc = gitops.clone(CLONE_URL, str(tmp_path))
    assert TOKEN not in proc.stderr
    assert proc.returncode == 128
    assert proc.args == ["git", "clone", CLONE_URL, str(tmp_path)]


def test_c2_a_real_failed_clone_shows_no_token(tmp_path):
    proc = gitops.clone("--", REFUSED_URL, str(tmp_path / "x"))
    print("c2 masked stderr:", repr(proc.stderr))
    assert proc.returncode != 0
    assert TOKEN not in proc.stderr


def test_c3_a_clone_that_exits_zero_is_unchanged(monkeypatch, tmp_path):
    stub_clone(monkeypatch, 0, "Cloning into 'x'...\n")
    assert gitops.clone("--", CLONE_URL, str(tmp_path)).stderr == "Cloning into 'x'...\n"


def test_c4_a_binary_clone_is_untouched(monkeypatch, tmp_path):
    raw = f"fatal: unable to access '{CLONE_URL}/': 403".encode()
    stub_clone(monkeypatch, 128, raw)
    proc = gitops.clone("--", CLONE_URL, str(tmp_path), binary=True)
    assert proc.stderr is raw
    assert proc.returncode == 128


def test_c5_clone_repo_shows_no_token_in_its_stderr_part(tmp_path):
    """Only the stderr part (after the first newline) is checked: the subject line still names the URL as given,
    and masking that subject is the next change, not this one."""
    with pytest.raises(SystemExit) as caught:
        gitops.clone_repo(REFUSED_URL, tmp_path / "x")
    message = str(caught.value)
    assert "\n" in message
    assert TOKEN not in message.split("\n", 1)[1]
