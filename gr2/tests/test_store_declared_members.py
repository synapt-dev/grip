"""`store init` reads member PATHS from the root's declared spec, and accepts
explicit ones, before it falls back to direct-child discovery.

WHY THIS EXISTS (measured 2026-09-28, on a root whose members sit one level down). `_discover_members`
iterated `root.iterdir()` and nothing else, so a root whose members sit one level down --
a workspace whose two member checkouts sit one level down (`core/config` and
`team-b/config`) -- exited 4 with "no sibling git repositories found to store". The
control that found it: the SAME member clone, same bytes and same origins, moved to depth 1
in a second copy inited rc 0. So the first real gripspace could not be adopted at all, and
the message said nothing about the paths that were declared and ignored.

The rule this file pins: declared paths win over guessing, explicit paths win over both, and
a spec that names members is never answered with "nothing found".
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _member_clone(root: Path, rel: str, bare: Path) -> Path:
    """A member checkout at <root>/<rel> with a local bare remote as its origin.

    A LOCAL bare remote, not a github URL: the native verbs fetch before they judge coverage,
    and a fictional remote would make every row here fail for a reason that is not this story.
    """
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    clone = root / rel
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True, text=True)
    _git(clone, "checkout", "-q", "-b", "main")
    (clone / "README.md").write_text(f"{rel}\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", f"member {rel}")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone


def _spec(root: Path, entries: list[tuple[str, str]]) -> None:
    """The alpha spec the root declares, in the shape a real workspace carries before adoption."""
    lines = ['workspace_name = "team"', ""]
    for name, path in entries:
        lines += [
            "[[repos]]",
            f'name = "{name}"',
            f'path = "{path}"',
            f'url = "git@github.com:synapt-dev/{name}.git"',
            'default_branch = "main"',
            "",
        ]
    (root / ".grip").mkdir(exist_ok=True)
    (root / ".grip" / "workspace_spec.toml").write_text("\n".join(lines))


def _store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _members_of(root: Path) -> dict[str, str]:
    doc = tomllib.loads((root / "grip.toml").read_text())
    return {entry["name"]: entry["path"] for entry in doc.get("members", [])}


def test_init_adopts_members_declared_at_depth_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape that found this: members one level down, declared by the root's own spec."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "core/config", tmp_path / "bare-core-config")
    _member_clone(root, "team-b/config", tmp_path / "bare-team-b-config")
    _spec(root, [("core-config", "core/config"), ("team-b-config", "team-b/config")])

    rc, out = _cli("store", "init", str(root))
    assert rc == 0, out
    assert _members_of(root) == {
        "core-config": "core/config",
        "team-b-config": "team-b/config",
    }, out


def test_direct_child_discovery_still_works_without_a_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL, unchanged by the fix: no spec, members as direct children."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "alpha", tmp_path / "bare-alpha")
    _member_clone(root, "beta", tmp_path / "bare-beta")

    rc, out = _cli("store", "init", str(root))
    assert rc == 0, out
    assert _members_of(root) == {"alpha": "alpha", "beta": "beta"}, out


def test_explicit_member_paths_are_accepted_without_a_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spec at all: the caller names the paths, and init does not guess."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "core/config", tmp_path / "bare-core-config")
    _member_clone(root, "team-b/config", tmp_path / "bare-team-b-config")

    rc, out = _cli(
        "store", "init", str(root), "--member", "core/config", "--member", "team-b/config"
    )
    assert rc == 0, out
    # The name an explicit path gets is the path with its separators dashed -- deterministic,
    # and the same string the alpha spec in this workspace already uses ("core-config").
    assert _members_of(root) == {
        "core-config": "core/config",
        "team-b-config": "team-b/config",
    }, out


def test_a_spec_naming_members_that_are_not_repos_refuses_naming_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec that names members is never answered with "nothing found".

    The declared paths exist but are plain directories, which is the state a stranger meets
    after a `git clone` of a root whose members are not part of its tree.
    """
    root = _store_root(tmp_path, monkeypatch)
    (root / "core" / "config").mkdir(parents=True)
    (root / "team-b" / "config").mkdir(parents=True)
    _spec(root, [("core-config", "core/config"), ("team-b-config", "team-b/config")])

    rc, out = _cli("store", "init", str(root))
    assert rc == 4, f"a declared member that is not a repo must be a refusal at 4, got {rc}: {out}"
    assert "core/config" in out and "team-b/config" in out, (
        f"the refusal must name the DECLARED paths it could not adopt, got: {out}"
    )
    assert "no sibling git repositories found to store" not in out, (
        "the declared-path refusal must not be the generic discovery message: "
        f"that is the answer this story removes. got: {out}"
    )


def _adopted_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A root that is ALREADY a git repo, which is where the trap below lives.

    Inside a non-checkout folder, `git rev-parse --show-toplevel` resolves to the ENCLOSING
    root, so a naive reader sees a success, an origin and a HEAD where there is no member.
    """
    root = tmp_path / "adopted"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("root\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "root")
    monkeypatch.chdir(root)
    return root


def test_a_named_path_that_is_a_plain_folder_refuses_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--member plain` must NOT record the root's own origin and HEAD as a member.

    Found by Apollo's r1 on this range: without the checkout check, this row's call exited 0
    and grip.toml carried a member named "plain" whose origin and pin were the ROOT's.
    """
    root = _adopted_root(tmp_path, monkeypatch)
    (root / "plain").mkdir()
    _git(root, "remote", "add", "origin", "https://example.invalid/root.git")

    rc, out = _cli("store", "init", str(root), "--member", "plain")
    assert rc == 4, f"a named path that is not a checkout must refuse at 4, got {rc}: {out}"
    assert "plain" in out, f"the refusal must name the path it could not adopt, got: {out}"
    assert not (root / "grip.toml").exists(), (
        "a refused init must not leave a spec behind recording the root as a member"
    )


def test_a_named_path_that_does_not_exist_refuses_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing path refuses NAMING it, not with the symptom's message."""
    root = _adopted_root(tmp_path, monkeypatch)
    _git(root, "remote", "add", "origin", "https://example.invalid/root.git")

    rc, out = _cli("store", "init", str(root), "--member", "core/absent")
    assert rc == 4, out
    assert "core/absent" in out, f"the refusal must name the missing path, got: {out}"
    assert "no origin remote" not in out, (
        "a path that is not there is not a path with a missing remote: "
        f"the message must say what is wrong. got: {out}"
    )


def test_a_named_path_that_escapes_the_root_refuses_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path outside the root must refuse at 4, not build a store that cannot commit.

    Measured by Stromus's r2 on this range: without the containment check, `--member
    ../outside` inited rc 0 and recorded member `..-outside`, and then `store commit` failed
    rc 5 with git's own `update-index: --cacheinfo cannot add ../outside`.
    """
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(tmp_path, "outside", tmp_path / "bare-outside")

    rc, out = _cli("store", "init", str(root), "--member", "../outside")
    assert rc == 4, f"a path outside the root must refuse at 4, got {rc}: {out}"
    assert "../outside" in out or "outside" in out, f"the refusal must name the path, got: {out}"
    assert not (root / "grip.toml").exists(), (
        "a refused init must not leave a spec behind for a store that could never commit"
    )


def test_a_spec_path_that_escapes_the_root_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same containment rule applies to a path the SPEC declares, not only a named one."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(tmp_path, "outside", tmp_path / "bare-outside")
    _spec(root, [("outside-config", "../outside")])

    rc, out = _cli("store", "init", str(root))
    assert rc == 4, f"a declared path outside the root must refuse at 4, got {rc}: {out}"
    assert "outside" in out, f"the refusal must name the declared path, got: {out}"


def test_two_member_paths_that_resolve_to_one_name_refuse_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`a/b` and `a-b` both dash to the name `a-b`, and a name key keeps only one.

    Measured by Stromus's r2: without this refusal, init recorded BOTH as `a-b` and
    `_native_members_at` builds `{name: pin}`, so any name-keyed read silently loses one.
    """
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "a/b", tmp_path / "bare-ab-slash")
    _member_clone(root, "a-b", tmp_path / "bare-ab-dash")

    rc, out = _cli("store", "init", str(root), "--member", "a/b", "--member", "a-b")
    assert rc == 4, f"a member name collision must refuse at 4, got {rc}: {out}"
    assert "a/b" in out and "a-b" in out, f"the refusal must name BOTH paths, got: {out}"


def test_the_same_path_named_twice_is_one_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the row above: naming the SAME path twice is de-duplication."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "core/config", tmp_path / "bare-core-config")

    rc, out = _cli("store", "init", str(root), "--member", "core/config", "--member", "core/config")
    assert rc == 0, out
    assert _members_of(root) == {"core-config": "core/config"}, out


def test_a_path_that_escapes_the_root_by_RESOLUTION_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment check's own case: no `..`, still outside.

    The `..` check and the resolve check overlap on `../outside`, which means a mutation of
    either one alone leaves that row green and neither is witnessed. This row separates them:
    a link inside the root pointing at a checkout outside it has no `..` part, so only the
    resolved-containment check can refuse it.
    """
    root = _store_root(tmp_path, monkeypatch)
    outside = _member_clone(tmp_path, "outside", tmp_path / "bare-outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    rc, out = _cli("store", "init", str(root), "--member", "escape")
    assert rc == 4, f"a path resolving outside the root must refuse at 4, got {rc}: {out}"
    assert "escape" in out, f"the refusal must name the path, got: {out}"
    assert not (root / "grip.toml").exists(), "a refused init must not leave a spec behind"
