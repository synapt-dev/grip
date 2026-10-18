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
