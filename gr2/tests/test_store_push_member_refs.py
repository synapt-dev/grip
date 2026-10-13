"""Section 9: `store push --atomic` adds the same-repo members' refs to the ONE atomic push that
carries the root branch and its shadow. A same-repo member is one whose origin url is the root's origin url (the same
string); its pin goes up as `refs/heads/<its ref>`. Members in their own repositories are never pushed by the root.

The fixture's member `gamma` lives in the root's own repository on branch `lib`, tracking `origin/wip`, so its pin
(wip's tip) is covered while origin's `lib` is still behind it: only a push that carries the member ref moves `lib`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run
from tests.test_store_shadow_s1 import MODES, SHADOW, _refs

LIB = "refs/heads/lib"


def _same_repo_store(tmp_path: Path, mode: str, member_branch: str = "lib",
                     member_url: str | None = None) -> tuple[Path, Path]:
    origin = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(origin))
    seed = tmp_path / "seed-root"
    run(tmp_path, "git", "clone", str(origin), str(seed))
    configure_identity(seed)
    git(seed, "commit", "--allow-empty", "-m", "lib")
    git(seed, "push", "origin", "HEAD:refs/heads/lib")
    git(seed, "commit", "--allow-empty", "-m", "wip")
    git(seed, "push", "origin", "HEAD:refs/heads/wip")
    root = tmp_path / "workspace"
    root.mkdir()
    remote, _ = make_member(tmp_path, "alpha")
    run(root, "git", "clone", str(remote), "alpha")
    run(root, "git", "clone", str(origin), "gamma")
    for name in ("alpha", "gamma"):
        configure_identity(root / name)
    git(root / "gamma", "checkout", "-q", "-B", member_branch, "origin/wip")
    if member_url is not None:
        git(root / "gamma", "remote", "set-url", "origin", member_url)
    gr2(root, "init")
    configure_identity(root)
    if mode == "full":
        text = (root / "grip.toml").read_text(encoding="utf-8")
        (root / "grip.toml").write_text('mode = "full"\n' + text, encoding="utf-8")
    gr2(root, "commit", "-m", "first")
    git(root, "remote", "add", "origin", str(origin))
    return root, origin


def _refuse(origin: Path, ref: str) -> None:
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(f'#!/bin/sh\nwhile read old new ref; do [ "$ref" = "{ref}" ] && '
                    f'{{ echo "test remote refuses $ref" >&2; exit 1; }}; done\nexit 0\n')
    hook.chmod(0o755)


def _pin(root: Path) -> str:
    return git(root / "gamma", "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("mode", MODES)
def test_m1_atomic_push_carries_the_same_repo_member_ref(tmp_path, mode):
    root, origin = _same_repo_store(tmp_path, mode)
    wip = _refs(origin)["refs/heads/wip"]

    result = gr2(root, "push", "--atomic", "--json")

    head = git(root, "rev-parse", "HEAD").stdout.strip()
    remote = _refs(origin)
    assert remote == {"refs/heads/main": head, SHADOW: head, LIB: _pin(root), "refs/heads/wip": wip}, \
        "branch, shadow and gamma's ref moved; alpha (its own repository) is never pushed to the root's origin"
    assert json.loads(result.stdout)["member_refs"] == [{"name": "gamma", "pin": _pin(root), "ref": LIB}]


@pytest.mark.parametrize("mode", MODES)
def test_m2_plain_push_leaves_member_refs_alone(tmp_path, mode):
    root, origin = _same_repo_store(tmp_path, mode)
    lib = _refs(origin)[LIB]
    assert lib != _pin(root), "the row needs origin's lib behind the pin"

    gr2(root, "push")

    remote = _refs(origin)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    assert remote[LIB] == lib and remote["refs/heads/main"] == head and remote[SHADOW] == head, remote


@pytest.mark.parametrize("mode", MODES)
def test_m3_a_refused_member_ref_moves_nothing(tmp_path, mode):
    root, origin = _same_repo_store(tmp_path, mode)
    before = _refs(origin)
    _refuse(origin, LIB)

    result = gr2(root, "push", "--atomic", check=False)

    assert result.returncode != 0
    assert LIB in result.stderr, result.stderr
    assert _refs(origin) == before, "the root branch and its shadow do not move without the member ref"


@pytest.mark.parametrize("mode", MODES)
def test_m4_a_member_past_its_pin_on_origin_moves_nothing(tmp_path, mode):
    """The member ref is pushed without force: a member whose branch on origin moved past the recorded pin is a
    non-fast-forward, and the whole push is refused rather than rewinding the member."""
    root, origin = _same_repo_store(tmp_path, mode)
    seed = tmp_path / "seed-root"
    git(seed, "fetch", "-q", "origin")
    git(seed, "checkout", "-q", "-B", "lib", "origin/wip")
    git(seed, "commit", "--allow-empty", "-m", "lib moved past the pin")
    git(seed, "push", "origin", "lib")
    before = _refs(origin)

    result = gr2(root, "push", "--atomic", check=False)

    assert result.returncode != 0
    assert LIB in result.stderr, result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_m5_a_same_repo_member_on_the_root_branch_is_refused_by_name(tmp_path, mode):
    root, origin = _same_repo_store(tmp_path, mode, member_branch="main")
    before = _refs(origin)

    result = gr2(root, "push", "--atomic", check=False)

    assert result.returncode == 4, (result.returncode, result.stderr)
    assert "gamma" in result.stderr and "root's own branch main" in result.stderr, result.stderr
    assert _refs(origin) == before


# Inclusion stays exact-string; a member whose origin is the root's by another
# string (one trailing `/`, no `.git`) is refused under --atomic, exit 4, naming the member and both strings.
ALIASES = {"slash": lambda origin: f"{origin}/", "nodotgit": lambda origin: str(origin)[:-len(".git")]}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("alias", sorted(ALIASES))
def test_m6_an_aliased_same_repo_member_is_refused_by_name(tmp_path, mode, alias):
    origin = tmp_path / "root.git"
    spelled = ALIASES[alias](origin)
    root, _ = _same_repo_store(tmp_path, mode, member_url=spelled)
    before = _refs(origin)

    result = gr2(root, "push", "--atomic", check=False)

    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert "gamma" in result.stderr, result.stderr
    assert repr(spelled) in result.stderr and repr(str(origin)) in result.stderr, result.stderr
    assert _refs(origin) == before, "nothing moves when a member is refused"


@pytest.mark.parametrize("mode", MODES)
def test_m7_control_the_exact_string_member_is_still_pushed(tmp_path, mode):
    """The control for m6: the same fixture with gamma's origin set to the root's exact string pushes, and alpha
    (a genuinely different repository) is still left out silently."""
    origin = tmp_path / "root.git"
    root, _ = _same_repo_store(tmp_path, mode, member_url=str(origin))

    result = gr2(root, "push", "--atomic", "--json")

    assert result.returncode == 0
    assert _refs(origin)[LIB] == _pin(root)
    assert json.loads(result.stdout)["member_refs"] == [{"name": "gamma", "pin": _pin(root), "ref": LIB}]
