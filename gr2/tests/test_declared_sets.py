"""A member of a declared set merges only with the set's own record, even as the only member of its review.

THE STORY (Shape 2): five authors each review ONE member of a change that only works together. Every review is a
single-member review, so `review merge` required only `test`, and merge 1 of 5 landed alone. A set is declared once in
the tracked grip.toml (`[[sets]]`), the gate reads it, and a member of a declared set needs a `declared_set` record
that a set check over a ROOT COMMIT writes, for a root commit pinning EVERY member of the set.

Smallest proof first: five single-member reviews, no record, merge 1 refuses; with the record, all five merge.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, declared_sets, merge_gate, set_check
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

runner = CliRunner()
PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]
SET = ["alpha", "beta", "gamma", "delta", "epsilon"]


def _refs(author):
    out = git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX)
    return set(out.split()) if out else set()


def set_block(name, members):
    return f'[[sets]]\nname = "{name}"\nmembers = {json.dumps(members)}\n'


def declare(author, name, members):
    text = (author / "grip.toml").read_text()
    (author / "grip.toml").write_text(text.rstrip("\n") + "\n\n" + set_block(name, members))


def redeclare(author, name, members):
    """Replace the declared sets in the working grip.toml (the file is edited by hand)."""
    text = (author / "grip.toml").read_text()
    head = text[: text.index("[[sets]]")].rstrip("\n")
    (author / "grip.toml").write_text(head + "\n\n" + set_block(name, members))


def store_commit(author, message):
    result = runner.invoke(app, ["store", "commit", "-m", message, "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return git(author, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Five set members plus `solo` (in no set): a bare remote and one reviewed feature head each, one single-member
    review each, a passing `test` record at every head, and a committed grip.toml that declares the set.

    The order is the one the bind rule needs: a head is pinned BEFORE it is pushed, so the store records the feature
    branch as each member's ref and the root commit is made after the pushes."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    author = tmp_path / "author"
    author.mkdir()
    members = {}
    for name in [*SET, "solo"]:
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "--bare", "-b", "main", remote)
        seed.mkdir()
        git(seed, "init", "-b", "main")
        (seed / "payload.txt").write_text(f"{name} base\n")
        git(seed, "add", "payload.txt")
        git(seed, "commit", "-m", "base")
        base = git(seed, "rev-parse", "HEAD")
        git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        members[name] = dict(repo=author / name, remote=remote, base=base)
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    monkeypatch.chdir(author)
    for name, m in members.items():
        repo = m["repo"]
        git(repo, "checkout", "-b", "feat")
        (repo / "payload.txt").write_text(f"{name} reviewed\n")
        git(repo, "add", "payload.txt")
        git(repo, "commit", "-m", "reviewed")
        m["head"] = git(repo, "rev-parse", "HEAD")
        before = _refs(author)
        made = runner.invoke(app, ["review", "bind", "--repo", name, "--remote", str(m["remote"]), "--base", m["base"],
                                   "--head", m["head"], "--ref", "refs/heads/main", "--source", str(repo)])
        assert made.exit_code == 0, made.output
        (ref,) = _refs(author) - before
        m["review"] = "gr:" + ref.rsplit("/", 1)[1]
        published = runner.invoke(app, ["review", "publish", m["review"], "--remote", str(m["remote"])])
        assert published.exit_code == 0, published.output
        git(repo, "push", "origin", "feat")
        check_records.run_check(repo, str(m["remote"]), m["head"], "test", PASS)
    text = (author / "grip.toml").read_text().replace('ref = "main"', 'ref = "feat"').replace('upstream = "origin/main"', 'upstream = "origin/feat"')
    (author / "grip.toml").write_text(text)
    declare(author, "core", SET)
    root = store_commit(author, "the change, every pin")
    return dict(author=author, members=members, root=root)


def merge(w, name):
    return merge_gate.review_merge(w["author"], w["members"][name]["review"], into="main", feature="feat")


def untouched(w, names=SET):
    return all(git(w["members"][n]["remote"], "rev-parse", "main") == w["members"][n]["base"] for n in names)


def record(w, argv=PASS, commit=None):
    return set_check.run_declared_set_check(w["author"], "core", commit or w["root"], argv)


# ── the story ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_shape_2_merge_one_of_five_refuses_when_the_set_has_no_record(world):
    """RED on dev: each review is single-member, so only `test` was required and merge 1 landed alone."""
    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("declared_set_absent"), receipt
    assert "alpha" in receipt["members"][0]["refused"] and "core" in receipt["members"][0]["refused"]
    assert untouched(world)


def test_with_the_set_recorded_over_a_root_commit_every_merge_proceeds(world):
    out = record(world)

    assert out["result"] == "pass" and sorted(m["member"] for m in out["members"]) == sorted(SET), out
    for name in SET:
        code, receipt = merge(world, name)
        assert code == merge_gate.EXIT_MERGED, (name, receipt)


def test_a_member_in_no_declared_set_merges_with_no_set_record(world):
    code, receipt = merge(world, "solo")

    assert code == merge_gate.EXIT_MERGED, receipt
    assert receipt["members"][0].get("declared_set") is None


def test_a_failing_set_check_is_recorded_and_the_merge_refuses(world):
    out = record(world, FAIL)

    assert out["result"] == "fail"
    code, receipt = merge(world, "alpha")
    assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_fail"), receipt
    assert untouched(world)


def _same_pins_new_commit(world):
    """An empty commit: the same gitlinks under a new commit id."""
    git(world["author"], "commit", "-q", "--no-verify", "--allow-empty", "-m", "same pins, new commit")
    again = git(world["author"], "rev-parse", "HEAD")
    assert again != world["root"]
    return again


def test_a_pass_at_another_commit_with_the_same_pins_does_not_outvote_a_failure(world):
    """A failure belongs to the pins it ran over; an empty commit gives the same pins a new id, and a retry there is not a fix."""
    assert record(world, FAIL)["result"] == "fail"
    again = _same_pins_new_commit(world)
    assert record(world, PASS, commit=again)["result"] == "pass"

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_fail"), receipt
    assert untouched(world)


def test_a_failure_at_another_commit_with_the_same_pins_outvotes_an_earlier_pass(world):
    assert record(world, PASS)["result"] == "pass"
    again = _same_pins_new_commit(world)
    assert record(world, FAIL, commit=again)["result"] == "fail"

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_fail"), receipt


def test_a_failure_over_pins_that_have_since_changed_does_not_block_a_pass(world):
    """Fixing the code moves a pin; the failure at the old pins stays on record and a pass at the new ones stands."""
    author = world["author"]
    heads = sorted(f"{n}:{world['members'][n]['head']}" for n in SET)
    old = sorted(f"{n}:{world['members'][n]['base'] if n == 'beta' else world['members'][n]['head']}" for n in SET)
    rows = [
        dict(name=declared_sets.DECLARED_SET_CHECK, result="fail", set_name="core", root_commit="1" * 40,
             declared_members=sorted(SET), heads=old),
        dict(name=declared_sets.DECLARED_SET_CHECK, result="pass", set_name="core", root_commit=world["root"],
             declared_members=sorted(SET), heads=heads),
    ]
    declaration = declared_sets.read(author)

    why = declared_sets.judge(author, declaration, declaration.set_named("core"), "alpha",
                              world["members"]["alpha"]["head"], rows)

    assert why is None, why


# ── what the record must cover ───────────────────────────────────────────────────────────────────────────────────────


def _four_of_five(world):
    """A root commit that pins only four of the five members."""
    author = world["author"]
    git(author, "rm", "--cached", "-q", "epsilon")
    git(author, "commit", "-q", "--no-verify", "-m", "four pins")
    return git(author, "rev-parse", "HEAD")


def test_a_root_commit_that_pins_four_of_five_is_refused_by_the_check_naming_the_missing_member(world):
    four = _four_of_five(world)

    with pytest.raises(check_records.CheckRefused, match=r"declared_set_member_unpinned.*epsilon"):
        record(world, commit=four)


def test_a_record_for_a_root_commit_that_pins_four_of_five_does_not_satisfy_the_gate(world):
    """The check refuses to write it, so a forged record stands in: the gate judges the commit, not the record."""
    four = _four_of_five(world)
    for name in SET:
        m = world["members"][name]
        heads = sorted(f"{n}:{world['members'][n]['head']}" for n in SET[:-1])
        row = dict(v=1, head=m["head"], observed_head=m["head"], name=declared_sets.DECLARED_SET_CHECK, result="pass",
                   exit_code=0, set_name="core", root_commit=four, declared_members=sorted(SET), heads=heads)
        check_records.publish_observation(m["repo"], str(m["remote"]), row)

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("declared_set_member_unpinned"), receipt
    assert "epsilon" in receipt["members"][0]["refused"]
    assert untouched(world)


def test_a_record_naming_a_root_commit_this_workspace_does_not_have_is_refused(world):
    """Five workspaces: the merging one reads its own commits, so a root commit it never saw covers nothing."""
    stranger = "0123456789abcdef0123456789abcdef01234567"
    for name in SET:
        m = world["members"][name]
        heads = sorted(f"{n}:{world['members'][n]['head']}" for n in SET)
        row = dict(v=1, head=m["head"], observed_head=m["head"], name=declared_sets.DECLARED_SET_CHECK, result="pass",
                   exit_code=0, set_name="core", root_commit=stranger, declared_members=sorted(SET), heads=heads)
        check_records.publish_observation(m["repo"], str(m["remote"]), row)

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_root_unknown"), receipt


def test_an_edited_declaration_does_not_ride_an_old_record(world):
    record(world)
    redeclare(world["author"], "core", SET[:-1])  # epsilon leaves the set
    store_commit(world["author"], "the set is declared differently")

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("declared_set_changed"), receipt
    assert untouched(world)


def test_a_record_pinning_another_head_for_this_member_is_refused(world):
    """The record is judged against the commit's gitlink for THIS member, not against the record's own say-so."""
    author, m = world["author"], world["members"]["alpha"]
    git(author, "update-index", "--cacheinfo", f"160000,{m['base']},alpha")
    git(author, "commit", "-q", "--no-verify", "-m", "alpha pinned at its base")
    moved = git(author, "rev-parse", "HEAD")
    row = dict(name=declared_sets.DECLARED_SET_CHECK, result="pass", set_name="core", root_commit=moved,
               declared_members=sorted(SET),
               heads=sorted(f"{n}:{m['base'] if n == 'alpha' else world['members'][n]['head']}" for n in SET))
    declaration = declared_sets.read(author)

    why = declared_sets.judge(author, declaration, declaration.set_named("core"), "alpha", m["head"], [row])

    assert why is not None and why.startswith("declared_set_head_mismatch"), why
    assert "alpha" in why


def test_a_record_whose_heads_are_not_what_its_root_commit_pins_is_refused(world):
    """The record's own head list is checked against the commit it names; a forged list describes another commit."""
    author, alpha = world["author"], world["members"]["alpha"]
    heads = {n: world["members"][n]["head"] for n in SET}
    heads["beta"] = "0" * 40
    row = dict(name=declared_sets.DECLARED_SET_CHECK, result="pass", set_name="core", root_commit=world["root"],
               declared_members=sorted(SET), heads=sorted(f"{n}:{h}" for n, h in heads.items()))
    declaration = declared_sets.read(author)

    why = declared_sets.judge(author, declaration, declaration.set_named("core"), "alpha", alpha["head"], [row])

    assert why is not None and why.startswith("declared_set_record_disagrees"), why


# ── the declaration ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_store_commit_keeps_the_declared_sets_it_did_not_write(world):
    """`store commit` regenerates grip.toml from the members; the tables the owner wrote by hand must survive it."""
    kept = declared_sets.read(world["author"], "HEAD")

    assert [(s.name, list(s.members)) for s in kept.sets] == [("core", sorted(SET))], kept
    assert "[[sets]]" in git(world["author"], "show", "HEAD:grip.toml")


def test_store_commit_carries_a_malformed_set_so_the_reader_refuses_it_by_name(world):
    """A hand-edit mistake must not become a deleted declaration: the bad table is committed as written and refused."""
    author = world["author"]
    text = (author / "grip.toml").read_text()
    (author / "grip.toml").write_text(text.rstrip("\n") + '\n\n[[sets]]\nname = "bad"\nmembers = "alpha"\n')

    store_commit(author, "a set with a members string")

    assert 'name = "bad"' in git(author, "show", "HEAD:grip.toml")
    with pytest.raises(declared_sets.DeclaredSetError, match="'bad'"):
        declared_sets.read(author)


MEMBERS = [{"name": n, "path": n, "remotes": {"origin": f"/r/{n}"}} for n in ("a", "b", "c")]


def test_a_set_naming_an_unknown_member_is_refused_at_read():
    with pytest.raises(declared_sets.DeclaredSetError, match="unknown member 'zz'"):
        declared_sets.parse({"members": MEMBERS, "sets": [{"name": "s", "members": ["a", "zz"]}]})


def test_a_member_in_two_sets_is_refused_at_read_naming_both():
    with pytest.raises(declared_sets.DeclaredSetError, match=r"'a' is in two sets \('s' and 't'\)"):
        declared_sets.parse({"members": MEMBERS, "sets": [{"name": "s", "members": ["a"]}, {"name": "t", "members": ["a", "b"]}]})


@pytest.mark.parametrize("sets, why", [
    ([{"members": ["a"]}], "needs a name"),
    ([{"name": "s", "members": []}], "needs members"),
    ([{"name": "s", "members": ["a", "a"]}], "lists a member twice"),
    ([{"name": "s", "members": ["a"]}, {"name": "s", "members": ["b"]}], "declared twice"),
    ("core", "array"),
])
def test_a_malformed_declaration_is_refused_at_read(sets, why):
    with pytest.raises(declared_sets.DeclaredSetError, match=why):
        declared_sets.parse({"members": MEMBERS, "sets": sets})


def test_an_invalid_declaration_stops_the_merge_before_anything_moves(world):
    redeclare(world["author"], "core", [*SET, "ghost"])
    git(world["author"], "add", "grip.toml")
    git(world["author"], "commit", "-q", "--no-verify", "-m", "a set naming a member that is not there")

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_PARTIAL and "declared_sets_invalid" in receipt["refused"], receipt
    assert "ghost" in receipt["refused"]
    assert untouched(world)


def test_the_check_refuses_an_undeclared_set_name(world):
    with pytest.raises(check_records.CheckRefused, match="declared_set_unknown"):
        set_check.run_declared_set_check(world["author"], "nope", world["root"], PASS)


# ── the verb: check set --root-commit ────────────────────────────────────────────────────────────────────────────────


def _check(world, *args):
    return runner.invoke(app, ["check", "set", *args, "--", *PASS], catch_exceptions=False)


def test_check_set_root_commit_writes_the_record_and_the_merges_then_proceed(world):
    out = _check(world, str(world["author"]), "--root-commit", world["root"], "--set", "core")

    assert out.exit_code == 0, out.output
    body = json.loads(out.stdout)
    assert body["set"] == "core" and body["root_commit"] == world["root"] and body["result"] == "pass", body
    assert sorted(m["member"] for m in body["members"]) == sorted(SET)
    code, receipt = merge(world, "alpha")
    assert code == merge_gate.EXIT_MERGED, receipt


def test_check_set_root_commit_takes_the_root_as_dash_c_too(world):
    out = _check(world, "-C", str(world["author"]), "--root-commit", world["root"], "--set", "core")

    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["result"] == "pass"


def test_check_set_refuses_a_review_beside_root_commit(world):
    out = _check(world, str(world["author"]), world["members"]["alpha"]["review"], "--root-commit", world["root"], "--set", "core")

    assert out.exit_code != 0 and "no review" in out.output, out.output


def test_check_set_refuses_set_without_root_commit(world):
    out = _check(world, str(world["author"]), world["members"]["alpha"]["review"], "--set", "core")

    assert out.exit_code == 2 and "set_applies_to_root_commit" in out.stdout, out.output


def test_check_set_root_commit_that_pins_four_of_five_exits_2_naming_the_member(world):
    four = _four_of_five(world)

    out = _check(world, str(world["author"]), "--root-commit", four, "--set", "core")

    assert out.exit_code == 2, out.output
    assert "declared_set_member_unpinned" in out.stdout and "epsilon" in out.stdout, out.stdout


def test_check_set_root_commit_needs_a_set_name(world):
    out = _check(world, str(world["author"]), "--root-commit", world["root"])

    assert out.exit_code == 2 and "declared_set_check_needs_a_set" in out.stdout, out.output
