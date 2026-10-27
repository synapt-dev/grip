"""`review pin` with no --ref targets the remote's HEAD branch, never an assumed dev.

  * W1 a remote with only main (HEAD -> main): no --ref reads main and the base check passes;
  * W2 an explicit --ref wins over the remote's HEAD;
  * W3 a remote whose HEAD names no branch refuses target_ref_missing and names --ref;
  * W4 an explicit --ref the remote lacks refuses target_ref_missing, not base_not_live_head;
  * W5 a rows-json row with no "ref" resolves the same way as W1;
  * W6 the one command a refusal prints carries no unquoted value (only the --ref <branch> placeholder);
  * W7 `review repin` with no --ref checks the frozen dir before it resolves the origin's HEAD.
  * W8 with no --ref, a remote that cannot be read refuses remote_unreadable, never target_ref_missing (W3 is the
    other side: a readable remote whose HEAD names no branch); the pin and repin each;
  * W9 a slow remote (its HEAD read would outlast a short bound) still resolves its HEAD branch: the read runs under
    the pin's own ls-remote bound, so slow never reads as "names no branch" (pin and repin each).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


def _remote_dir(remote: str) -> Path:
    return Path(remote.removeprefix("file://"))


def _set_head(remote: str, target: str) -> None:
    subprocess.run(["git", "--git-dir", str(_remote_dir(remote)), "symbolic-ref", "HEAD", target], check=True)


def _pin(ws: Path, remote: str, base: str, head: str, *extra: str) -> tuple[int, str]:
    return _cli("review", "pin", str(ws), "--repo", "alpha", "--remote", remote, "--base", base,
                "--head", head, "--source", str(ws / "alpha"), *extra)


def _ready(ws: Path) -> tuple[str, str, str]:
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    _set_head(remote, "refs/heads/main")
    return remote, base, head


def test_w1_no_ref_reads_the_remote_head_branch(two_member_ws: Path) -> None:
    remote, base, head = _ready(two_member_ws)
    code, out = _pin(two_member_ws, remote, base, head)
    assert code == 0, out
    assert out.strip().splitlines()[-1].startswith("gr:"), out


def test_w2_explicit_ref_wins_over_head(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    alpha = ws / "alpha"
    remote = _git_out(alpha, "remote", "get-url", "origin")
    _set_head(remote, "refs/heads/main")
    (alpha / "dev-only.txt").write_text("dev\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "dev only")
    dev_tip = _git_out(alpha, "rev-parse", "HEAD")
    _git(alpha, "push", "-q", "origin", "HEAD:refs/heads/dev")
    (alpha / "review-change.txt").write_text("under review\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "the change under review")
    head = _git_out(alpha, "rev-parse", "HEAD")
    code, out = _pin(ws, remote, dev_tip, head, "--ref", "refs/heads/dev")
    assert code == 0, out
    assert out.strip().splitlines()[-1].startswith("gr:"), out


def test_w3_a_remote_naming_no_head_branch_refuses_target_ref_missing(two_member_ws: Path) -> None:
    remote, base, head = _ready(two_member_ws)
    _set_head(remote, "refs/heads/no-such-branch")
    code, out = _pin(two_member_ws, remote, base, head)
    assert code != 0, out
    assert "target_ref_missing" in out and "--ref <branch>" in out, out
    assert "base_not_live_head" not in out, out


def test_w4_an_explicit_ref_the_remote_lacks_refuses_target_ref_missing(two_member_ws: Path) -> None:
    remote, base, head = _ready(two_member_ws)
    code, out = _pin(two_member_ws, remote, base, head, "--ref", "refs/heads/dev")
    assert code != 0, out
    assert "target_ref_missing" in out and "refs/heads/dev" in out, out
    assert "base_not_live_head" not in out, out


def test_w5_a_rows_json_row_with_no_ref_resolves_the_remote_head(two_member_ws: Path, tmp_path: Path) -> None:
    ws = two_member_ws
    remote, base, head = _ready(ws)
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"key": "alpha", "remote": remote, "base": base, "head": head,
                                 "source": str(ws / "alpha")}]))
    code, out = _cli("review", "pin", str(ws), "--rows-json", str(rows))
    assert code == 0, out
    assert out.strip().splitlines()[-1].startswith("gr:"), out


def test_w6_the_printed_command_carries_no_unquoted_value(two_member_ws: Path, tmp_path: Path) -> None:
    ws = two_member_ws
    remote, base, head = _ready(ws)
    spaced = tmp_path / "a remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(_remote_dir(remote)), str(spaced)], check=True)
    code, out = _pin(ws, str(spaced), base, head, "--ref", "refs/heads/dev")
    assert code != 0 and "target_ref_missing" in out, out
    # The command a user is told to run is the placeholder alone; the spaced path is named, never inside it.
    command = out.split("pass ", 1)[1]
    assert command.startswith("--ref <branch>") and "a remote" not in command, out


def test_w7_repin_with_no_ref_on_a_missing_frozen_dir_refuses_frozen_dir_missing(tmp_path: Path) -> None:
    # --repo has no origin, so resolving HEAD first would refuse target_ref_missing instead.
    code, out = _cli("review", "repin", str(tmp_path / "absent"), "--repo", str(tmp_path),
                     "--out-dir", str(tmp_path / "out"))
    assert code != 0 and "frozen_dir_missing" in out and "target_ref_missing" not in out, out


def test_w8_no_ref_on_an_unreadable_remote_refuses_remote_unreadable(two_member_ws: Path) -> None:
    remote, base, head = _ready(two_member_ws)
    gone = _remote_dir(remote)
    gone.rename(gone.with_name(gone.name + ".moved"))
    code, out = _pin(two_member_ws, remote, base, head)
    assert code != 0, out
    assert "remote_unreadable" in out and "target_ref_missing" not in out, out


def test_w8_repin_with_no_ref_on_an_unreadable_origin_refuses_remote_unreadable(tmp_path: Path) -> None:
    import pytest

    from gr2.python_cli import review_rebind as rb
    from tests.test_review_rebind import _frozen_range, _origin_with_base

    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    origin.rename(origin.with_name("origin.moved"))
    with pytest.raises(rb.RebindRefused) as refused:
        rb.rebind(frozen, tmp_path / "author", "", tmp_path / "frozen-v2")
    assert refused.value.code == "remote_unreadable", refused.value


def _slow_symref(monkeypatch) -> None:
    """A --symref HEAD read answers only under the full ls-remote bound; under any shorter bound it times out (rc 124,
    as gitops.git reports an expiry)."""
    from gr2.python_cli import gitops

    real = gitops.git

    def slow(cwd, *args, timeout=gitops.DEFAULT_TIMEOUT):
        if "--symref" in args and (timeout is not None and timeout < gitops.DEFAULT_TIMEOUT):
            return subprocess.CompletedProcess(["git", *args], 124, "", "timed out")
        return real(cwd, *args, timeout=timeout)

    monkeypatch.setattr(gitops, "git", slow)


def test_w9_a_slow_remote_still_resolves_its_head_branch(two_member_ws: Path, monkeypatch) -> None:
    remote, base, head = _ready(two_member_ws)
    _slow_symref(monkeypatch)
    code, out = _pin(two_member_ws, remote, base, head)
    assert code == 0 and "target_ref_missing" not in out, out


def test_w9_repin_on_a_slow_origin_still_resolves_its_head_branch(tmp_path: Path, monkeypatch) -> None:
    from gr2.python_cli import review_rebind as rb
    from tests.test_review_rebind import _frozen_range, _origin_with_base

    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    _slow_symref(monkeypatch)
    res = rb.rebind(frozen, tmp_path / "author", "", tmp_path / "frozen-v2")
    assert res.outcome == "base_unchanged", res

