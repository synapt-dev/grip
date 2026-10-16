"""`store push` lease 2 (plan section 5): each member's pin must fast-forward from the pin origin's shadow records.

The walk is local, in the member's own clone, by the org cut's walker. A commit the clone does not hold is fetched
from the member's remote once; still missing is refused as unverifiable. `--allow-rewind <member>` takes a rewind or
a rewrite for that member only, and the result names it.

A fixture that records a rewound or rewritten pin locally passes `store commit --allow-rewind`: store commit refuses a
pin that moves backwards on its own, so lease 2 is what these rows put under test, not the commit-time check.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import MODES, SHADOW, _advance, _refs, _refuse_shadow, _store

MAIN = "refs/heads/main"
REWOUND = "refs/dev.synapt.grip/rewound"


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _two_pushes(tmp_path: Path, mode: str) -> tuple[Path, Path, str, str]:
    """A root pushed twice: alpha at p0 in the first record, at p1 (a fast-forward) in the second, origin at both."""
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    p0 = _head(root / "alpha")
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha moved")
    gr2(root, "push")
    return root, origin, p0, _head(root / "alpha")


def _forget(repo: Path, sha: str) -> None:
    """`repo` no longer holds `sha`: every ref and reflog entry that reached it dropped, then pruned."""
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0


def _rewrite(root: Path, tmp_path: Path, base: str) -> str:
    """alpha's origin main replaced by a commit off `base`, so the recorded pin is no ancestor of the new one."""
    work = root / "alpha"
    git(work, "reset", "-q", "--hard", base)
    (work / "rewritten.txt").write_text("rewritten\n")
    git(work, "add", "rewritten.txt")
    git(work, "commit", "-q", "-m", "rewritten")
    git(work, "push", "-q", "--force", "origin", "HEAD:main")
    return _head(work)


@pytest.mark.parametrize("mode", MODES)
def test_b1_control_fast_forward_pushes_and_names_nothing(tmp_path, mode):
    root, origin, _, p1 = _two_pushes(tmp_path, mode)
    head = _head(root)
    assert _refs(origin) == {MAIN: head, SHADOW: head}
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha again")
    result = gr2(root, "push", "--json")
    assert json.loads(result.stdout)["rewound"] == []
    assert _refs(origin) == {MAIN: _head(root), SHADOW: _head(root)}


@pytest.mark.parametrize("mode", MODES)
def test_b2_rewind_is_refused_by_member_and_both_shas(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"alpha: rewind: its pin {p0} does not descend from the pin origin's shadow records, {p1}" in result.stderr
    assert "--allow-rewind alpha takes it" in result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_b3_rewrite_is_refused_by_name(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    q = _rewrite(root, tmp_path, p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha rewritten")
    result = gr2(root, "push", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"alpha: rewrite: its pin {q} does not descend from the pin origin's shadow records, {p1}" in result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_b4_allow_rewind_takes_it_and_the_result_names_it(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    result = gr2(root, "push", "--allow-rewind", "alpha", "--json")
    payload = json.loads(result.stdout)
    assert payload["rewound"] == [{"name": "alpha", "kind": "rewind", "old": p1, "new": p0}]
    tip = _head(root)
    record = f"{REWOUND}/{tip}"
    assert payload["rewound_record"] == record
    assert set(_refs(origin)) == {MAIN, SHADOW, record}
    assert (_refs(origin)[MAIN], _refs(origin)[SHADOW]) == (tip, tip)


@pytest.mark.parametrize("mode", MODES)
def test_b5_allow_rewind_takes_only_the_member_it_names(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    result = gr2(root, "push", "--allow-rewind", "beta", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert "alpha: rewind:" in result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_b6_a_recorded_pin_not_held_is_fetched_then_walked(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    work = root / "alpha"
    git(work, "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")   # store commit's own coverage fetch runs here, so forget p1 after it
    git(work, "update-ref", "-d", "refs/remotes/origin/main")
    _forget(work, p1)
    # --recurse=none: no coverage fetch runs first, so only the walk's own fetch can bring p1 back (under the default
    # the coverage fetch of origin restores it before the walk, and a walk with no fetch would pass unseen: mutant
    # no-fetch survived that form). p1 still sits on alpha's remote main, so the walk fetches it and names the
    # rewind, not "cannot verify".
    result = gr2(root, "push", "--recurse=none", check=False)
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert f"alpha: rewind: its pin {p0} does not descend from the pin origin's shadow records, {p1}" in result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_b7_a_recorded_pin_gone_everywhere_cannot_be_verified(tmp_path, mode):
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    q = _rewrite(root, tmp_path, p0)
    work = root / "alpha"
    _forget(work, p1)
    _forget(tmp_path / "alpha.git", p1)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha rewritten, p1 gone")
    result = gr2(root, "push", check=False)
    assert result.returncode == 5, (result.returncode, result.stderr)
    assert f"cannot verify alpha: {p1} not held" in result.stderr
    assert "nothing was pushed" in result.stderr
    assert _refs(origin) == before


@pytest.mark.parametrize("mode", MODES)
def test_b8_a_rewind_taken_is_written_down_on_origin_with_the_tip(tmp_path, mode):
    # A rewind needs --allow-rewind, and is written down. The record is a commit on origin whose
    # parent is the tip and whose tree is the tip's (no object the tip does not carry), naming member, kind, both pins.
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    gr2(root, "push", "--allow-rewind", "alpha")
    tip = _head(root)
    record = f"{REWOUND}/{tip}"
    assert git(origin, "rev-parse", f"{record}^").stdout.strip() == tip
    assert git(origin, "rev-parse", f"{record}^{{tree}}").stdout.strip() == git(origin, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
    assert git(origin, "rev-list", "--count", record).stdout.strip() == str(int(git(origin, "rev-list", "--count", tip).stdout.strip()) + 1)
    assert f"alpha: rewind: {p1} -> {p0}" in git(origin, "log", "-1", "--format=%B", record).stdout


@pytest.mark.parametrize("mode", MODES)
def test_b8_the_record_rides_the_one_atomic_push(tmp_path, mode):
    # Origin refuses the shadow: nothing moves, the record included (it lands with the tip or not at all).
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    before = _refs(origin)
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    _refuse_shadow(origin)
    result = gr2(root, "push", "--allow-rewind", "alpha", check=False)
    assert result.returncode != 0
    assert _refs(origin) == before


def _remote_main(member: Path) -> str:
    url = git(member, "remote", "get-url", "origin").stdout.strip()
    return git(member, "ls-remote", url, "refs/heads/main").stdout.split()[0]


def _beta_ahead_alpha_back(tmp_path: Path) -> tuple[Path, Path, str]:
    # Full mode only: compat's store commit refuses a pin its upstream does not cover, so the state is unreachable.
    root, origin, p0, _ = _two_pushes(tmp_path, "full")
    beta = root / "beta"
    before = _remote_main(beta)
    (beta / "local.txt").write_text("not on beta's remote yet\n")
    git(beta, "add", "local.txt")
    git(beta, "commit", "-q", "-m", "beta local")
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back, beta ahead")
    return root, origin, before


def test_b9_on_demand_publishes_no_member_when_lease_2_refuses(tmp_path):
    # Regression: on-demand pushed beta, THEN lease 2 refused alpha saying nothing was pushed.
    root, origin, beta_before = _beta_ahead_alpha_back(tmp_path)
    refs_before = _refs(origin)
    result = gr2(root, "push", "--recurse=on-demand", check=False)
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "alpha" in result.stderr and "nothing was pushed" in result.stderr
    assert _refs(origin) == refs_before
    assert _remote_main(root / "beta") == beta_before, "beta's remote moved before lease 2 refused alpha"


def test_b9_control_on_demand_still_publishes_once_the_walk_passes(tmp_path):
    root, origin, beta_before = _beta_ahead_alpha_back(tmp_path)
    result = gr2(root, "push", "--recurse=on-demand", "--allow-rewind", "alpha", check=False)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert _remote_main(root / "beta") == _head(root / "beta") != beta_before


def _declared(verb: str) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """The push shape as api/cli.api publishes it: the top-level keys, each array's element keys, and each enum."""
    from gr2.python_cli import grip_cli

    top: set[str] = set()
    elements: dict[str, set[str]] = {}
    enums: dict[str, set[str]] = {}
    for line in grip_cli._STORE_JSON_SHAPES[verb]:
        path, _, note = line.partition("  ")
        head, _, tail = path.lstrip(".").partition("[].")
        top.add(head.removesuffix("[]"))
        if tail:
            elements.setdefault(head, set()).add(tail)
        if note.startswith("enum("):
            enums[path] = set(note[len("enum("):-1].split(","))
    return top, elements, enums


def _holds_to_the_declared_shape(payload: dict) -> None:
    top, elements, enums = _declared("push")
    assert set(payload) == top
    for key, wanted in elements.items():
        for entry in payload[key]:
            assert set(entry) == wanted, key
    for path, values in enums.items():
        head, _, tail = path.lstrip(".").partition("[].")
        seen = [entry[tail] for entry in payload[head]] if tail else [payload[head]]
        assert set(seen) <= values, (path, seen)


@pytest.mark.parametrize("mode", MODES)
def test_push_json_carries_exactly_the_declared_keys_and_enums(tmp_path, mode):
    """Every key `store push --json` emits is declared (api/cli.api is built from the declaration), every array element
    carries the declared keys, and every enum value is in its declared set: a plain push, a `--recurse=none` push (its
    members are `unchecked`), and an `--allow-rewind` push (its `rewound` list is filled)."""
    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha ahead")
    _holds_to_the_declared_shape(json.loads(gr2(root, "push", "--json").stdout))
    (root / "alpha" / "again.txt").write_text("again\n")   # `_advance` writes the same bytes twice
    git(root / "alpha", "add", "again.txt")
    git(root / "alpha", "commit", "-q", "-m", "again")
    git(root / "alpha", "push", "-q", "origin", "main")
    gr2(root, "commit", "-m", "alpha ahead again")
    unchecked = json.loads(gr2(root, "push", "--recurse=none", "--json").stdout)
    _holds_to_the_declared_shape(unchecked)
    assert {member["state"] for member in unchecked["members"]} == {"unchecked"}
    git(root / "alpha", "reset", "-q", "--hard", p0)
    gr2(root, "commit", "--allow-rewind", "-m", "alpha back")
    rewound = json.loads(gr2(root, "push", "--allow-rewind", "alpha", "--json").stdout)
    _holds_to_the_declared_shape(rewound)
    assert rewound["rewound"]


@pytest.mark.parametrize("mode", MODES)
def test_b10_lease_two_resolves_each_member_against_the_other_members_claims(tmp_path, mode, monkeypatch):
    """Lease 2 finds a member's clone the way every other verb does: with every OTHER member's two coordinates
    claimed, so a member's name fallback can never land in a neighbour's checkout."""
    from gr2.python_cli import grip_cli

    root, origin, p0, p1 = _two_pushes(tmp_path, mode)
    recorded = _refs(origin)[SHADOW]
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "alpha again")
    seen: dict[str, set[Path]] = {}
    real = grip_cli._native_member_working_root

    def spy(root_arg, member, claimed):
        seen[member["name"]] = set(claimed)
        return real(root_arg, member, claimed)

    monkeypatch.setattr(grip_cli, "_native_member_working_root", spy)
    assert grip_cli._member_rewinds(root, _head(root), recorded, []) == []
    assert set(seen) == {"alpha"}, seen
    assert (root / "beta").resolve() in seen["alpha"], seen
