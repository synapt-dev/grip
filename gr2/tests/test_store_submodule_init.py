"""After `store commit` or `store materialize`, plain `git submodule update --remote <member>` needs no hand-run init.

THE DEFECT, measured on grip dev 73d95ebb: the generated `.gitmodules` names each member, but no member was registered
in the root's `.git/config`, so `git submodule update --remote alpha` refused with "Submodule path 'alpha' not
initialized" until the user ran `git submodule init alpha`. Git 2.50 says so on stderr and EXITS 0, leaving the member
where it was, so a script that checks the exit code reads the no-op as success (measured; the control row pins it).
That is the plain-git spelling of "advance each member to its branch", and it needed a step the user had to know about.

THE RULE: wherever gr2 writes the `.gitmodules` it owns (store commit) or materializes a root that carries one (store
materialize), it registers each member with `git submodule init -- <path>`, which writes only the root's own
`submodule.<name>.*` config. Nothing is registered where gr2 owns no `.gitmodules`: a full-mode store (it projects
none) and a root whose `.gitmodules` is an adopted superproject's own file (gr2 leaves that file to its owner).

Fixtures are real git with bare local origins and no network. Git refuses a local-path submodule URL by default, so
the `update --remote` rows name `protocol.file.allow`.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_store_gitmodules_branch import _on_dev, _push_one, _store
from tests.test_store_git_native_smoke import git, gr2, run


def _update_remote(root: Path, member: str):
    return run(root, "git", "-c", "protocol.file.allow=always", "submodule", "update", "--remote", member, check=False)


def _registered(root: Path, name: str) -> bool:
    return git(root, "config", "--get", f"submodule.{name}.url", check=False).returncode == 0


def test_after_store_commit_a_plain_update_remote_needs_no_init(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    assert _registered(root, "beta")
    dev_tip = _push_one(tmp_path, "dev")

    moved = _update_remote(root, "beta")

    assert moved.returncode == 0 and "not initialized" not in moved.stderr, f"{moved.stdout}\n{moved.stderr}"
    assert git(root / "beta", "rev-parse", "HEAD").stdout.strip() == dev_tip


def test_after_store_materialize_a_plain_update_remote_needs_no_init(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "-q", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", str(root_remote), str(fresh))
    assert gr2(fresh, "materialize").returncode == 0
    assert _registered(fresh, "beta")
    dev_tip = _push_one(tmp_path, "dev")

    moved = _update_remote(fresh, "beta")

    assert moved.returncode == 0 and "not initialized" not in moved.stderr, f"{moved.stdout}\n{moved.stderr}"
    assert git(fresh / "beta", "rev-parse", "HEAD").stdout.strip() == dev_tip


def test_a_root_cloned_without_a_checkout_materializes_and_registers_nothing(tmp_path: Path) -> None:
    """With --no-checkout the root's index is empty, so no plain `git submodule` verb can name a member there.
    Materialize still succeeds and registers nothing, rather than failing after its work is done."""
    root = _store(tmp_path, _on_dev)
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "-q", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", "--no-checkout", str(root_remote), str(fresh))

    done = gr2(fresh, "materialize")
    assert done.returncode == 0
    assert not _registered(fresh, "beta")
    assert "not registered" not in done.stderr, done.stderr   # skipped, not attempted and refused


def test_control_without_the_registration_plain_git_refuses(tmp_path: Path) -> None:
    """Without this the rows above could pass because update --remote never needed an init in this fixture. Git exits 0
    here, so the row reads the message and the member's HEAD, never the exit code."""
    root = _store(tmp_path, _on_dev)
    git(root, "config", "--remove-section", "submodule.beta", check=False)
    assert not _registered(root, "beta")
    before = git(root / "beta", "rev-parse", "HEAD").stdout.strip()
    dev_tip = _push_one(tmp_path, "dev")

    refused = _update_remote(root, "beta")

    assert "not initialized" in refused.stderr, refused.stderr
    assert git(root / "beta", "rev-parse", "HEAD").stdout.strip() == before != dev_tip


def test_a_foreign_gitmodules_is_not_registered(tmp_path: Path) -> None:
    """An adopted superproject's `.gitmodules` belongs to its owner: gr2 neither rewrites it nor registers from it."""
    def foreign(member: Path) -> None:
        _on_dev(member)
        (member.parent / ".gitmodules").write_text('[submodule "elsewhere"]\n\tpath = elsewhere\n\turl = ../x.git\n')

    root = _store(tmp_path, foreign)
    assert "elsewhere" in (root / ".gitmodules").read_text()   # precondition: the file is the foreign one
    again = gr2(root, "commit", "-m", "again")   # a no-op commit still reaches the registration step
    assert not _registered(root, "elsewhere")
    assert not _registered(root, "beta")
    assert "not registered" not in again.stderr, again.stderr   # skipped, not attempted and refused


def test_a_full_mode_store_registers_nothing(tmp_path: Path) -> None:
    """Full mode projects no `.gitmodules` (its gitlinks name field-commits, not member heads), so there is nothing a
    plain `git submodule` verb could follow and nothing to register, after commit or after materialize."""
    from tests.test_store_full_mode_carrier import _advance, _full_store
    from tests.test_store_git_native_smoke import configure_identity

    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    committed = gr2(root, "commit", "-m", "layered")
    assert committed.returncode == 0
    assert git(root, "config", "--get-regexp", r"^submodule\.", check=False).stdout == ""
    assert "not registered" not in committed.stderr, committed.stderr   # skipped, not attempted and refused
    git(tmp_path, "clone", "-q", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)

    done = gr2(fresh, "materialize")
    assert done.returncode == 0
    assert git(fresh, "config", "--get-regexp", r"^submodule\.", check=False).stdout == ""
    assert "not registered" not in done.stderr, done.stderr


def test_a_member_git_refuses_to_register_is_named_and_the_commit_still_succeeds(tmp_path: Path) -> None:
    """Registration is a convenience, never a gate. A member name git calls suspicious (one carrying `..`) cannot be
    registered; the commit still succeeds and stderr names the member with the hand step."""
    from tests.test_store_break_attempts import _set_member_name

    root = _store(tmp_path, _on_dev)
    spec = root / "grip.toml"
    spec.write_text(_set_member_name(spec.read_text(), "beta", "../beta"))

    done = gr2(root, "commit", "-m", "suspicious name", check=False)

    assert done.returncode == 0, done.stderr
    assert "beta: not registered as a submodule" in done.stderr, done.stderr
    assert "git submodule init beta" in done.stderr, done.stderr
