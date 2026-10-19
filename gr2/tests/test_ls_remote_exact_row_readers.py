"""ls-remote matches its pattern by tail, so a remote holding refs/heads/a/<ref> beside <ref> lists the lookalike too,
and it may sort first. Each reader here must take the row named exactly <ref>, never the first row or any row."""

from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip, lane_defaults, push, sync_wait
from tests.test_review_bind_verify import _fixture_remote, _grip_ws, _row
from tests.test_review_bind_verify import _git as _bind_git
from tests.test_store_full_mode_coverage import _vanished_covering_ref
from tests.test_store_git_native_smoke import configure_identity, git, gr2


def _remote_with_feat(tmp_path: Path, *, lookalike: bool) -> tuple[Path, Path, str, str]:
    """A bare remote with refs/heads/feat at base B; with ``lookalike``, refs/heads/a/refs/heads/feat at local commit L,
    which the clone never pushed to feat. Returns (clone, remote, B, L)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    clone = tmp_path / "clone"
    git(tmp_path, "init", "-b", "main", str(clone))
    configure_identity(clone)
    git(clone, "commit", "--allow-empty", "-m", "base")
    base = git(clone, "rev-parse", "HEAD").stdout.strip()
    git(clone, "remote", "add", "origin", str(remote))
    git(clone, "push", "origin", "HEAD:refs/heads/feat")
    git(clone, "commit", "--allow-empty", "-m", "local")
    local = git(clone, "rev-parse", "HEAD").stdout.strip()
    if lookalike:
        git(clone, "push", "origin", "HEAD:refs/heads/a/refs/heads/feat")
    return clone, remote, base, local


def _receipt(local: str) -> push.PushReceipt:
    return push.PushReceipt(remote="origin", branch="other", local_sha=local, remote_sha=local,
                            set_upstream=False, force_with_lease=False)


def test_lane_branch_uncovered_reads_the_exact_row_beside_a_lookalike(tmp_path):
    clone, remote, base, local = _remote_with_feat(tmp_path, lookalike=True)
    listed = git(clone, "ls-remote", "--heads", "--", "origin", "refs/heads/feat").stdout
    assert listed.split()[1] == "refs/heads/a/refs/heads/feat", listed

    assert push._lane_branch_uncovered(clone, _receipt(local), "feat") == f"head {local} is not on origin/feat; push it first"


def test_lane_branch_uncovered_without_a_lookalike_says_push_it_first(tmp_path):
    clone, remote, base, local = _remote_with_feat(tmp_path, lookalike=False)

    assert push._lane_branch_uncovered(clone, _receipt(local), "feat") == f"head {local} is not on origin/feat; push it first"


def test_lane_defaults_ls_remote_reads_the_exact_row_beside_a_lookalike(tmp_path):
    clone, remote, base, local = _remote_with_feat(tmp_path, lookalike=True)

    assert lane_defaults._ls_remote(str(remote), "refs/heads/feat", 10.0) == base


def test_lane_defaults_ls_remote_without_a_lookalike(tmp_path):
    clone, remote, base, local = _remote_with_feat(tmp_path, lookalike=False)

    assert lane_defaults._ls_remote(str(remote), "refs/heads/feat", 10.0) == base


def test_lane_defaults_ls_remote_reads_a_lone_lookalike_as_no_such_ref(tmp_path):
    clone, remote, base, local = _remote_with_feat(tmp_path, lookalike=True)
    git(clone, "push", "origin", "--delete", "feat")

    assert lane_defaults._ls_remote(str(remote), "refs/heads/feat", 10.0) == ""


def _materialize_after_vanish(tmp_path: Path, *, lookalike: bool) -> str:
    fresh, pin = _vanished_covering_ref(tmp_path, with_layer=False)
    if lookalike:
        url = git(fresh / "alpha", "remote", "get-url", "origin").stdout.strip()
        git(fresh / "alpha", "push", url, "HEAD:refs/heads/a/refs/heads/side")
    made = gr2(fresh, "materialize", check=False)
    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 3, out
    assert pin in out or "cannot be fetched" in out, out
    return out


def test_store_covering_ref_vanished_beside_a_lookalike(tmp_path):
    out = _materialize_after_vanish(tmp_path, lookalike=True)

    assert "full_mode_covering_ref_vanished" in out, out


def test_store_covering_ref_vanished_without_a_lookalike(tmp_path):
    out = _materialize_after_vanish(tmp_path, lookalike=False)

    assert "full_mode_covering_ref_vanished" in out, out


def _review_remote(tmp_path: Path, *, lookalike: bool) -> tuple[Path, str, str, str]:
    """A bare remote with refs/heads/dev at base A and an unpushed head B; with ``lookalike``, B is pushed to
    refs/heads/a/refs/heads/dev, which ls-remote lists before dev. Returns (workspace, remote, A, B)."""
    remote, base, head = _fixture_remote(tmp_path)
    if lookalike:
        _bind_git(tmp_path / "work", "push", "origin", "HEAD:refs/heads/a/refs/heads/dev")
        listed = _bind_git(tmp_path, "ls-remote", "--", remote, "refs/heads/dev")
        assert listed.split()[1] == "refs/heads/a/refs/heads/dev", listed
    return _grip_ws(tmp_path), remote, base, head


def test_remote_head_reads_the_exact_row_beside_a_lookalike(tmp_path):
    ws, remote, base, head = _review_remote(tmp_path, lookalike=True)

    assert grip._remote_head(ws, remote, "refs/heads/dev") == base


def test_review_receipt_liveness_sees_dev_unmoved_beside_a_lookalike(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    ws = _grip_ws(tmp_path)
    commit = grip.create_review_bind_commit(ws, [_row(remote, base, head)])
    _bind_git(tmp_path / "work", "push", "origin", "HEAD:refs/heads/a/refs/heads/dev")

    receipt = grip.build_review_receipt(ws, commit, actor="t", verdict="ratify", axes={}, run_results={})

    assert receipt["liveness"][0]["live_remote_head"] == base
    assert receipt["liveness"][0]["state"] == "equal"


def test_remote_head_without_a_lookalike(tmp_path):
    ws, remote, base, head = _review_remote(tmp_path, lookalike=False)

    assert grip._remote_head(ws, remote, "refs/heads/dev") == base


_LINE_SEP_MAIN = "refs/heads/main\u2028/refs/heads/main"


def _sync_remote(tmp_path: Path, *, main: bool, lookalike: bool) -> tuple[Path, sync_wait.Member, str]:
    """A bare remote holding refs/heads/main at commit X when ``main``, and refs/heads/main<U+2028>/refs/heads/main at
    X when ``lookalike``: ls-remote's pattern refs/heads/main tail-matches that name, and str.splitlines cuts it at
    U+2028 into a row named refs/heads/main. Returns (workspace, member, X)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    clone = tmp_path / "clone"
    git(tmp_path, "init", "-b", "main", str(clone))
    configure_identity(clone)
    git(clone, "commit", "--allow-empty", "-m", "x")
    x = git(clone, "rev-parse", "HEAD").stdout.strip()
    git(clone, "push", str(remote), "HEAD:refs/heads/side")
    if main:
        git(remote, "update-ref", "refs/heads/main", x)
    if lookalike:
        git(remote, "update-ref", _LINE_SEP_MAIN, x)
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, sync_wait.Member("m", "m", str(remote), "main", None), x


def test_sync_wait_read_remote_reads_a_lone_line_separator_lookalike_as_no_main(tmp_path):
    ws, member, x = _sync_remote(tmp_path, main=False, lookalike=True)

    assert sync_wait._read_remote(ws, member, 30.0) == (None, "no refs/heads/main on the remote")


def test_sync_wait_read_remote_reads_plain_main(tmp_path):
    ws, member, x = _sync_remote(tmp_path, main=True, lookalike=False)

    assert sync_wait._read_remote(ws, member, 30.0) == (x, "")


def test_sync_wait_read_remote_reads_main_beside_a_line_separator_lookalike(tmp_path):
    ws, member, x = _sync_remote(tmp_path, main=True, lookalike=True)

    assert sync_wait._read_remote(ws, member, 30.0) == (x, "")
