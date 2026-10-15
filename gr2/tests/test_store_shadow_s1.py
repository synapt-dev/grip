"""Section 9, S1: HEAD is the shadow. `store commit` names the commit it makes twice, the branch and
`refs/dev.synapt.grip/__branches__/<branch>/SHADOW`, and `store push` (plain, no flag) moves
both on origin in ONE atomic push.

The smallest proof (full-mode push plan, 2026-10-12): a bare remote whose pre-receive hook refuses the shadow ref is the
witness (NEITHER ref moves, the verb exits non-zero naming the shadow); the same push with no hook is the control (both
refs move to the same sha); pushing the refs one at a time is the mutant (main moves, the shadow does not: the witness
reds). Every row runs in both store modes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run

SHADOW = "refs/dev.synapt.grip/__branches__/main/SHADOW"
OLD_ROOT = "refs/dev.synapt.grip/__shadows__/"  # the S1 name, since retired
MODES = ["compat", "full"]


def _store(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    """Two members, a store init in `mode`, one recorded root commit, and an empty bare remote as the root's origin."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    gr2(root, "init")
    configure_identity(root)
    if mode == "full":
        text = (root / "grip.toml").read_text(encoding="utf-8")
        (root / "grip.toml").write_text('mode = "full"\n' + text, encoding="utf-8")
    gr2(root, "commit", "-m", "first")
    origin = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    return root, origin


def _advance(root: Path, member: str) -> None:
    work = root / member
    (work / "moved.txt").write_text(f"{len(list(work.glob('*.txt')))}\n")
    git(work, "add", "moved.txt")
    git(work, "commit", "-m", "moved")
    git(work, "push", "origin", "main")


def _refs(repo: Path) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines()
    return dict(line.split(" ", 1) for line in out)


def _refuse_shadow(origin: Path) -> None:
    hook = origin / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\nwhile read old new ref; do case "$ref" in refs/dev.synapt.grip/__branches__/*)\n'
                    '  echo "test remote refuses $ref" >&2; exit 1;; esac; done\nexit 0\n')
    hook.chmod(0o755)


@pytest.mark.parametrize("mode", MODES)
def test_s1_commit_names_the_shadow_the_same_commit(tmp_path, mode):
    root, _ = _store(tmp_path, mode)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert _refs(root)[SHADOW] == head

    _advance(root, "alpha")
    gr2(root, "commit", "-m", "second")

    second = git(root, "rev-parse", "HEAD").stdout.strip()
    assert second != head and _refs(root)[SHADOW] == second, "each verb commit moves the shadow with it"
    assert not [ref for ref in _refs(root) if ref.startswith(OLD_ROOT)], "the retired name is never written"


@pytest.mark.parametrize("mode", MODES)
def test_s1_hand_commit_is_not_named_by_the_shadow(tmp_path, mode):
    root, _ = _store(tmp_path, mode)
    git(root, "commit", "--no-verify", "--allow-empty", "-m", "hand")  # full mode's hook refuses a hand commit

    assert _refs(root)[SHADOW] != git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("mode", MODES)
def test_s1_control_plain_push_moves_both_refs_to_one_sha(tmp_path, mode):
    root, origin = _store(tmp_path, mode)

    result = gr2(root, "push")

    head = git(root, "rev-parse", "HEAD").stdout.strip()
    remote = _refs(origin)
    assert remote.get("refs/heads/main") == head and remote.get(SHADOW) == head, (remote, result.stdout)
    assert not [ref for ref in remote if ref.startswith(OLD_ROOT)], remote


@pytest.mark.parametrize("mode", MODES)
def test_s1_witness_refused_shadow_moves_neither_ref(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    before = _refs(origin)
    _advance(root, "beta")
    gr2(root, "commit", "-m", "second")
    _refuse_shadow(origin)

    result = gr2(root, "push", check=False)

    assert result.returncode != 0
    assert SHADOW in result.stderr, result.stderr
    assert _refs(origin) == before, "an atomic push the remote refused must move neither ref"


@pytest.mark.parametrize("mode", MODES)
def test_s1_witness_without_atomic_support_moves_nothing(tmp_path, mode):
    """A remote that does not advertise atomic pushes is refused, never degraded to one ref at a time."""
    root, origin = _store(tmp_path, mode)
    git(origin, "config", "receive.advertiseAtomic", "false")

    result = gr2(root, "push", check=False)

    assert result.returncode != 0
    assert "atomic" in result.stderr, result.stderr
    assert _refs(origin) == {}, "nothing moves on a remote that cannot take both refs at once"


@pytest.mark.parametrize("mode", MODES)
def test_s1_commit_on_a_root_detached_by_store_checkout_lands_and_names_no_shadow(tmp_path, mode):
    """`store checkout <rev>` detaches the root; a `store commit` there is made (exit 0, HEAD moves) and, naming no
    branch, reads and moves no shadow."""
    root, _ = _store(tmp_path, mode)
    first = git(root, "rev-parse", "HEAD").stdout.strip()
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "second")
    gr2(root, "checkout", first)
    assert git(root, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0, "store checkout left the root detached"
    git(root / "alpha", "checkout", "-q", "main")   # alpha's own main, which origin holds: a pin change to record
    shadows = {ref: sha for ref, sha in _refs(root).items() if ref.startswith("refs/dev.synapt.grip/__branches__/")}
    result = gr2(root, "commit", "-m", "detached", check=False)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert git(root, "rev-parse", "HEAD").stdout.strip() != first
    assert {ref: sha for ref, sha in _refs(root).items()
            if ref.startswith("refs/dev.synapt.grip/__branches__/")} == shadows
