"""Each site that compares member paths compares them as member keys (``keys.member_path_key``), so ``./alpha`` and
``alpha/`` are the member ``alpha``. The key does not fold ``..``: ``a/../alpha`` is a different key, as git keeps it."""
from __future__ import annotations

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
    declaration = declared_sets.parse({"members": [{"name": "alpha", "path": "a/../alpha"}]})
    assert declaration.member_name("not-a-name", "alpha") is None
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
