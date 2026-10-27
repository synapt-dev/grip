"""`store check` in a plain clone, before materialize: a member that is not checked out names materialize, not push.

THE CAUSE: a plain `git clone` of a workspace leaves each member dir empty (or absent), and `git -C <member>` walks up
from an empty dir to the SUPERPROJECT. Coverage then fetched the root's origin and asked whether the member's pin was
on the root's upstream, which it never is, so check said "push it first" for a pin its own upstream already holds.
Coverage now refuses at 5 ("could not ask") when the member is not here, so a script reading 3 never pushes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _store(tmp_path: Path) -> Path:
    """One member cloned under a root, a store init, `mode = "full"` and one recorded root commit."""
    root = tmp_path / "workspace"
    root.mkdir()
    remote, _ = make_member(tmp_path, "alpha")
    run(root, "git", "clone", str(remote), "alpha")
    configure_identity(root / "alpha")
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text('mode = "full"\n' + text, encoding="utf-8")
    first = gr2(root, "commit", "-m", "first")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    return root


def _plain_clone(tmp_path: Path) -> Path:
    root = _store(tmp_path)
    git(tmp_path, "clone", str(root), "plain")
    plain = tmp_path / "plain"
    configure_identity(plain)
    return plain


def _out(done) -> str:
    return f"{done.stdout}\n{done.stderr}"


def _assert_names_materialize_not_push(done) -> None:
    out = _out(done)
    assert done.returncode == 5, out
    assert "gr2 store materialize" in out, out
    assert "push" not in out, out
    assert 'mode = "full"' not in out, out


def test_r1_an_empty_member_dir_names_materialize_not_push(tmp_path):
    plain = _plain_clone(tmp_path)
    assert (plain / "alpha").is_dir() and not any((plain / "alpha").iterdir()), "setup: the member dir must be empty"

    _assert_names_materialize_not_push(gr2(plain, "check", check=False))


def test_r2_a_removed_member_dir_names_materialize_not_push(tmp_path):
    plain = _plain_clone(tmp_path)
    shutil.rmtree(plain / "alpha", ignore_errors=True)
    assert not (plain / "alpha").exists(), "setup: the member dir must be gone"

    _assert_names_materialize_not_push(gr2(plain, "check", check=False))


def test_r3_a_real_unpushed_pin_still_says_push(tmp_path):
    root = _store(tmp_path)
    alpha = root / "alpha"
    (alpha / "unpushed.txt").write_text("unpushed\n")
    git(alpha, "add", "unpushed.txt")
    git(alpha, "commit", "-q", "-m", "unpushed")
    # Recorded while covered (so no layer carries it), then taken back off its upstream: the pin is on no remote.
    git(alpha, "push", "-q", "origin", "HEAD:main")
    recorded = gr2(root, "commit", "-m", "record the pin")
    assert recorded.returncode == 0, _out(recorded)
    git(alpha, "push", "-q", "-f", "origin", "HEAD~1:main")

    refused = gr2(root, "check", check=False)

    out = _out(refused)
    assert refused.returncode == 3, out
    assert "push it first" in out, out


def test_r4_after_materialize_check_passes(tmp_path):
    plain = _plain_clone(tmp_path)
    done = gr2(plain, "materialize")
    assert done.returncode == 0, _out(done)

    checked = gr2(plain, "check", check=False)

    out = _out(checked)
    assert checked.returncode == 0, out
    assert "Checked 1 member(s)" in out, out
