"""One canonical key per kind: the member-path kind. `keys.member_path_key` is the one spelling; the rows here
pin that spelling and each call site moved onto it."""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from gr2.python_cli import keys
from gr2.python_cli import review_allocation as allocation
from gr2.python_cli import store_hooks


@pytest.mark.parametrize(
    "spelling, key",
    [
        ("alpha", "alpha"),
        ("./alpha", "alpha"),
        ("alpha/", "alpha"),
        ("./alpha/", "alpha"),
        ("alpha//", "alpha"),
        ("group/./alpha", "group/alpha"),
        ("group//alpha/", "group/alpha"),
        (Path("alpha"), "alpha"),
        (PurePosixPath("group/alpha"), "group/alpha"),
        # git folds neither case nor `..`, so neither does the key.
        ("Alpha", "Alpha"),
        ("x/../alpha", "x/../alpha"),
    ],
)
def test_member_path_key_spells_paths_as_git_does(spelling, key):
    assert keys.member_path_key(spelling) == key


def test_store_hooks_git_path_is_the_one_key_not_a_copy():
    assert store_hooks._git_path is keys.member_path_key


def _allocated(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace, target = tmp_path / "workspace", tmp_path / "review"
    member = target / "alpha"
    workspace.mkdir()
    member.mkdir(parents=True)
    for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"),
                 ("commit", "-q", "--allow-empty", "-m", "seed")):
        subprocess.run(["git", "-C", str(member), *args], check=True, capture_output=True)
    allocation.record_created_allocation(workspace, target, "workspace", "review", [member], disposable=True)
    return workspace, target, member


@pytest.mark.parametrize("recorded", ["alpha", "./alpha", "alpha/"])
def test_member_relationship_matches_the_recorded_path_by_key(tmp_path, recorded):
    """review_allocation.require_member_relationship: a record spelling the member `./alpha` or `alpha/` names the
    same member as `alpha`; a raw string compare refused it as undeclared."""
    workspace, target, member = _allocated(tmp_path)
    doc = allocation.require_allocation(workspace, target)
    doc["members"][0]["path"] = recorded
    allocation.save_allocation(workspace, target, doc)
    allocation.require_member_relationship(workspace, target, "workspace", "review", member)


def test_member_relationship_still_refuses_a_member_not_declared(tmp_path):
    workspace, target, _ = _allocated(tmp_path)
    other = target / "beta"
    other.mkdir()
    with pytest.raises(allocation.ReviewAllocationError, match="does not declare this member"):
        allocation.require_member_relationship(workspace, target, "workspace", "review", other)
