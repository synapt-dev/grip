"""Each site that compares member paths compares them as member keys (``keys.member_path_key``), so ``./alpha`` and
``alpha/`` are the member ``alpha``. The key does not fold ``..``: ``a/../alpha`` is a different key, as git keeps it."""
from __future__ import annotations

import pytest

from gr2.python_cli import declared_sets


def _raw(path: str) -> declared_sets.Declaration:
    # built without the loader, so the stored path is exactly as spelled
    return declared_sets.Declaration((), {"alpha": {"path": path, "remote": ""}})


def test_a_member_stored_as_dot_slash_alpha_is_found_by_the_path_alpha():
    assert _raw("./alpha").member_name("not-a-name", "alpha") == "alpha"


def test_a_member_stored_as_alpha_slash_is_found_by_the_path_alpha():
    assert _raw("alpha/").member_name("not-a-name", "alpha") == "alpha"


def test_behaviour_change_a_dot_dot_path_no_longer_matches_the_member_it_resolves_to():
    # Before, the loader and the lookup both ran normpath, which folds `..`; the member key does not.
    with pytest.raises(declared_sets.DeclaredSetError, match="member_path_outside_root"):  # a '..' part leaves no member
        declared_sets.parse({"members": [{"name": "alpha", "path": "a/../alpha"}]})
    declaration = declared_sets.parse({"members": [{"name": "alpha", "path": "alpha"}]})
    assert declaration.member_name("not-a-name", "a/../alpha") is None
    assert declaration.member_name("not-a-name", "./alpha/") == "alpha"


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True, text=True)


def test_a_member_spelled_dot_slash_alpha_keeps_its_pin_and_its_declared_ref(tmp_path, monkeypatch):
    from gr2.python_cli import app as app_mod

    root = tmp_path / "root"
    member = root / "alpha"
    member.mkdir(parents=True)
    _git(member, "init", "-q", "-b", "main")
    _git(member, "commit", "-q", "--allow-empty", "-m", "m")
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitmodules").write_text('[submodule "alpha"]\n\tpath = ./alpha\n\turl = ./alpha\n\tbranch = dev\n')
    _git(root, "add", ".gitmodules", "alpha")
    _git(root, "commit", "-q", "-m", "root")
    pin = app_mod.repo_proto.root_gitlink_pins(root)["alpha"]
    seen = {}

    def spy(path, *, pinned=None, declared_ref=None):
        seen[path.name] = (pinned, declared_ref)
        return {}

    monkeypatch.setattr(app_mod.repo_proto, "declared_repo_state", spy)
    app_mod._scan_existing_repos(root)
    assert seen["alpha"] == (pin, "dev")


def test_an_unmaterialized_member_spelled_dot_slash_alpha_keeps_its_declared_url(tmp_path):
    from gr2.python_cli import app as app_mod

    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitmodules").write_text(
        '[submodule "alpha"]\n\tpath = ./alpha\n\turl = https://example.invalid/x/alpha\n\tbranch = dev\n')
    _git(root, "update-index", "--add", "--cacheinfo", "160000," + "ab" * 20 + ",alpha")
    _git(root, "add", ".gitmodules")
    _git(root, "commit", "-q", "-m", "root")
    (root / "alpha").mkdir()
    [entry] = app_mod._scan_existing_repos(root)
    assert (entry["path"], entry["url"], entry.get("ref")) == ("alpha", "https://example.invalid/x/alpha", "dev")


def test_a_grip_toml_dot_slash_alpha_carries_the_review_record_for_alpha(tmp_path, monkeypatch):
    from gr2.python_cli import grip as grip_mod
    from gr2.python_cli import grip_cli

    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "review")
    _git(root, "update-ref", grip_cli.REVIEW_REF_ROOT + "r1", "HEAD")
    pin = "ab" * 20
    monkeypatch.setattr(grip_mod, "show_review_commit",
                        lambda _root, _commit: {"members": [{"path": "alpha", "head": pin}]})
    records = grip_cli._member_records(root, {"path": "./alpha", "pin": pin})
    assert len(records) == 1


def _member(name: str, path: str) -> str:
    return f'[[members]]\nname = "{name}"\npath = "{path}"\n[members.remotes]\norigin = "https://example.invalid/x/{name}"\n'


def test_the_working_grip_toml_refuses_dot_slash_alpha_beside_alpha(tmp_path):
    import pytest

    from gr2.python_cli import grip_cli

    (tmp_path / "grip.toml").write_text(_member("alpha", "./alpha") + _member("beta", "alpha"))
    with pytest.raises(grip_cli.NativeStoreRefusal) as refusal:
        grip_cli._native_members(tmp_path)
    assert "grip.toml has duplicate member paths" in str(refusal.value)


def test_the_working_grip_toml_keeps_a_duplicate_name_on_different_paths(tmp_path):
    # Names are compared only for a selected commit; a duplicate name in the working document is the fallback's to
    # contain, so refusing it here would refuse the whole workspace for a collision only one fallback can act on.
    from gr2.python_cli import grip_cli

    (tmp_path / "grip.toml").write_text(_member("alpha", "alpha") + _member("alpha", "beta"))
    members = grip_cli._native_members(tmp_path)
    assert [m["path"] for m in members] == ["alpha", "beta"]


def test_a_selected_commit_still_refuses_a_duplicate_name(tmp_path, monkeypatch):
    import subprocess

    import pytest

    from gr2.python_cli import grip_cli

    pin = "1" * 40
    document = (_member("alpha", "alpha") + _member("alpha", "beta")).replace("[members.remotes]", f'pin = "{pin}"\n[members.remotes]')

    def store_git(_root, *args):
        out = document if args[0] == "show" else f"160000 commit {pin}\t{args[-1]}\n"
        return subprocess.CompletedProcess(list(args), 0, out, "")

    monkeypatch.setattr(grip_cli, "_store_git", store_git)
    with pytest.raises(grip_cli.NativeStoreRefusal) as refusal:
        grip_cli._native_members(tmp_path, "HEAD")
    assert "selected workspace commit has duplicate member names or paths" in str(refusal.value)


def test_a_name_that_spells_its_own_path_differently_claims_one_directory(tmp_path):
    # READ: resolve() already folds `./alpha` and `alpha`, so this held before the key too; it pins that the keyed
    # compare does not add a second claim or send the member to a fallback at its own directory.
    from gr2.python_cli import grip_cli

    member = {"name": "alpha", "path": "./alpha"}
    assert grip_cli._claimed_member_paths(tmp_path, [member]) == {(tmp_path / "alpha").resolve()}
    root = grip_cli._native_member_working_root(tmp_path, member, set())
    assert root.resolve() == (tmp_path / "alpha").resolve()


def test_clone_dot_slash_a_with_member_a_is_not_refused_before_the_clone(tmp_path, monkeypatch):
    from gr2.python_cli import grip_cli

    asked = []

    class Stop(Exception):
        pass

    def stop(_root):
        raise Stop

    monkeypatch.setattr(grip_cli, "_plan_clones", lambda _root, clones: list(clones))
    monkeypatch.setattr(grip_cli, "_discover_members", lambda _root, declared: asked.append(declared))
    monkeypatch.setattr(grip_cli, "_refuse_enclosing_repo", stop)
    try:
        grip_cli._native_store_init(tmp_path / "root", member_paths=["a"], clones=[("./a", "https://example.invalid/a")])
    except Stop:
        pass
    assert asked == []


def test_a_pending_marker_for_alpha_finds_the_spec_row_spelled_dot_slash_alpha(tmp_path, monkeypatch):
    from gr2.python_cli import spec_apply

    (tmp_path / "alpha").mkdir()
    ran = []
    spec = {"repos": [{"name": "alpha", "path": "./alpha", "url": "https://example.invalid/alpha"}]}
    monkeypatch.setattr(spec_apply, "build_plan", lambda _root: (spec, []))
    monkeypatch.setattr(spec_apply, "pending_members", lambda _root: ["alpha"])
    monkeypatch.setattr(spec_apply, "consent_member_key", lambda _root, _repo_root, name: name)
    monkeypatch.setattr(spec_apply, "consent_state", lambda _root, _key, _repo_root: ("bound",))
    monkeypatch.setattr(spec_apply, "_run_materialize_hooks",
                        lambda _root, _repo_root, name, _first, manual_hooks: ran.append(name) or {"projected_files": []})
    spec_apply.apply_plan(tmp_path, yes=True)
    assert ran == ["alpha"]
