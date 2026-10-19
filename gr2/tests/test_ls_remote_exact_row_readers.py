"""ls-remote matches its pattern by tail, so a remote holding refs/heads/a/<ref> beside <ref> lists the lookalike too,
and it may sort first. Each reader here must take the row named exactly <ref>, never the first row or any row."""

from __future__ import annotations

from pathlib import Path

from gr2.python_cli import lane_defaults, push
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
