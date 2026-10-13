"""The shared name validator: GIT-REF agrees with git, and the agent record's fence is the STRICT profile itself."""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import agent_record, names
from tests.hostile_names import HOSTILE_NAMES

# The E3 names, plus one name for each check-ref-format rule E3 does not reach, plus names that must stay legal.
EXTRA = [
    "feat/x", "release/1.x", "a-b_c.d", "x/.y", "x.lock/y", "a//b", "a/", "/a", "a\\b", "a*b", "a?b", "a[b",
    "a^b", "a\x7fb", "a\tb", "x.", "café", "日本", "@x", "x@", "a@b", "HEAD", ".hidden",
]
CORPUS = [name for _, name in HOSTILE_NAMES] + EXTRA


def _git_accepts(name: str) -> bool:
    done = subprocess.run(["git", "check-ref-format", "--allow-onelevel", name], capture_output=True)
    return done.returncode == 0


@pytest.mark.parametrize("name", CORPUS, ids=[f"{i}" for i in range(len(CORPUS))])
def test_git_ref_accepts_exactly_what_git_accepts_less_our_three_rules(name: str) -> None:
    """GIT-REF is git's one-level rules plus: printable, no leading '-', and (already git's) no edge space."""
    expected = _git_accepts(name) and name.isprintable() and not name.startswith("-")
    try:
        names.git_ref(name, "name")
        accepted = True
    except ValueError:
        accepted = False
    assert accepted == expected, (name, _git_accepts(name))


def test_git_ref_refuses_the_two_names_git_takes_in_other_forms() -> None:
    """`git check-ref-format --branch @` and `refs/heads/-flag` both exit 0, so neither form can stand in for this
    profile; both names are refused here, and U+2028, which every git form takes, is refused as unprintable."""
    for name in ("@", "-flag", "x y"):
        with pytest.raises(ValueError, match="is not a usable git name"):
            names.git_ref(name, "branch name")


def test_the_agent_record_fence_is_the_strict_profile() -> None:
    """One function, so the fence cannot drift from the profile Cloud's names are minted under."""
    assert agent_record._leaf is names.strict
    assert names.strict("STOOK", "ref") == "STOOK"
    for bad in ("stook", "Alice", "a..b", "x.", "x.lock", "a/b", "@", ""):
        with pytest.raises(ValueError):
            names.strict(bad, "ref")
