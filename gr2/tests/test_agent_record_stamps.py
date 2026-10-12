"""PER-FIELD STAMPS THROUGH THE APPEND: the per-field stamp experiment's rows re-passed with both
writers using agent_record.append on one record, not raw plumbing.

THE APPEND it writes through (fold() sits beside it, unused here):

    # gr2/python_cli/agent_record.py
    def append(repo: Path, agent: str, event: dict, ref: str = "STOOK", *, message: str,
               schema: Schema | None = None, saw: tuple[str, ...] = ()) -> Appended(commit, ordinal, ...)

A write is one Story event under the CALLER'S schema, carrying only the field it changed, with that
field's stamp {ts, rheon}. The caller never writes `by`: append sets by=agent in every stamp the event
carries (_stamped), so the name in the stamp is gr2's, and these rows test gr2's stamp.
  field layout (Story):      key, <Field>{value, stamp}       the stamp sits ON the field
  item layout  (ItemStory):  key, <Field> strings, stamp      one stamp for the item (R3's control)
A and B are in the same rheon and the same second, and neither saw the other. So each appends to its OWN
STOOK (own refs: never contended). The record is the union of their writes. Two writes merge clean
iff git merges their field trees clean: ``git merge-tree --write-tree --merge-base=<empty tree> A B``
(git 2.53: merge-tree takes bare trees here). The judge is git's merge, as in the experiment. The
trees it judges are the ones append wrote, read back from each writer's tip at the ordinal append
returned.

Rows ported from the stamp experiment (R1-R3) and its follow-up R4 (own_stamped), numbers kept:
100 rounds, keys item-<i>, A sets Status=In Progress; B sets Desk=desk-b (diff) or Status=Blocked
(same). Files backend: the experiment's rows name no backend. Each row has a mutant that reds it
(itemstamp, stampall, nostamp, and append-stamps-nothing), asserted to red exactly its declared rows.

THE GAP (named, not invented around): no fork. append gives a write one parent, the old tip of its
own ref (plus saw=). The experiment forked A and B off one BASE commit holding the story. Through
append, a shared STOOK serialises them by CAS (a SAW, not the concurrent case), and own STOOKs share
no base. So the base here is the EMPTY tree, each write is a delta, and "the record" is the union of
the deltas, not a merged commit on any ref.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"  # the caller's org namespace; gr2 ships none

ROUNDS = 100
TS = "2026-10-12T18:30:00Z"  # both writers in the same second of the same rheon
RHEON = "7"

STORY_PROTO = """syntax = "proto3";
package example.stamps.v1;

message Stamp {
  string by = 1;
  string ts = 2;
  string rheon = 3;
}

message Field {
  string value = 1;
  Stamp stamp = 2;
}

message Story {        // field layout: the stamp sits ON the field
  string key = 1;
  Field Title = 2;
  Field Status = 3;
  Field Desk = 4;
}

message ItemStory {    // item layout: one stamp for the item
  string key = 1;
  string Title = 2;
  string Status = 3;
  string Desk = 4;
  Stamp stamp = 5;
}
"""
SCHEMA = fd.parse_proto(STORY_PROTO)

# slice 1's entry names (number.wiretype_name), RAN against write_record
KEY, TITLE, STATUS, DESK, ISTAMP = "001.2_key", "002.2_Title", "003.2_Status", "004.2_Desk", "005.2_stamp"
VALUE, FSTAMP, BY = "001.2_value", "002.2_stamp", "001.2_by"

ENV = dict(os.environ, GIT_AUTHOR_NAME="stamps", GIT_AUTHOR_EMAIL="stamps@example.invalid",
           GIT_COMMITTER_NAME="stamps", GIT_COMMITTER_EMAIL="stamps@example.invalid")


@pytest.fixture
def agent_record():
    """Imported per test so each row goes RED on its own, naming the missing module."""
    return importlib.import_module("gr2.python_cli.agent_record")


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, env=ENV)


def make_repo(tmp_path: Path) -> Path:
    r = tmp_path / "files.git"
    subprocess.run(["git", "init", "-q", "--bare", "--ref-format=files", str(r)], check=True)
    return r


def change(layout: str, key: str, field: str, value: str) -> tuple[str, dict]:
    """One writer's event: only the field it changed, with its stamp. No `by`: append stamps the writer."""
    stamp = {"ts": TS, "rheon": RHEON}
    if layout == "field":
        return "Story", {"key": key, field: {"value": value, "stamp": stamp}}
    return "ItemStory", {"key": key, field: value, "stamp": stamp}


def written_tree(repo: Path, agent: str, got) -> str:
    """The field tree append wrote: entry f"{ordinal:07d}" of the commit it returned."""
    return git(repo, "rev-parse", f"{got.commit}^{{tree}}:{got.ordinal:07d}").stdout.strip()


def paths(repo: Path, tree: str) -> set[str]:
    return set(git(repo, "ls-tree", "-r", "--name-only", tree).stdout.split())


def run(agent_record, repo: Path, layout: str, which: str, rounds: int = ROUNDS) -> dict:
    """One round, through append: A and B each append one change to record item-<i> on their own
    STOOKs, then git merges the two field trees append wrote."""
    empty = subprocess.run(["git", "-C", str(repo), "mktree"], input="", capture_output=True, text=True,
                           check=True, env=ENV).stdout.strip()
    fa, fb = ("Status", "Desk") if which == "diff" else ("Status", "Status")
    va, vb = ("In Progress", "desk-b") if which == "diff" else ("In Progress", "Blocked")
    out = {"rounds": rounds, "clean": 0, "conflicts": 0, "both_kept": 0, "picked": 0,
           "own_stamped": 0, "only_own_field": 0, "conflict_paths": {}, "ordinals": {"a": [], "b": []}}
    for i in range(rounds):
        key = f"item-{i}"
        trees = {}
        for who, f, v in (("a", fa, va), ("b", fb, vb)):
            message, event = change(layout, key, f, v)
            got = agent_record.append(repo, who, event, org=ORG, message=message, schema=SCHEMA)
            out["ordinals"][who].append(got.ordinal)
            t = trees[who] = written_tree(repo, who, got)
            back = fd.read_record(repo, t, message, SCHEMA)
            fd.verify_tree(repo, t, message, SCHEMA)
            stamp = back[f].get("stamp") if layout == "field" else back.get("stamp")
            out["own_stamped"] += stamp == {"by": who, "ts": TS, "rheon": RHEON}
            out["only_own_field"] += set(back) == {"key", f, *(["stamp"] if layout == "item" else [])}
        m = git(repo, "merge-tree", "--write-tree", "--name-only", f"--merge-base={empty}",
                trees["a"], trees["b"], check=False)
        assert m.returncode in (0, 1), m.stderr
        lines = m.stdout.split("\n")
        if m.returncode == 1:
            out["conflicts"] += 1
            for p in lines[1:]:
                if not p:
                    break
                out["conflict_paths"][p] = out["conflict_paths"].get(p, 0) + 1
            continue
        out["clean"] += 1
        merged = fd.read_record(repo, lines[0], "Story" if layout == "field" else "ItemStory", SCHEMA)
        val = (lambda f: merged.get(f, {}).get("value")) if layout == "field" else (lambda f: merged.get(f))
        if which == "diff":
            out["both_kept"] += val(fa) == va and val(fb) == vb
        else:
            out["picked"] += 1  # a same-field change merged clean: one side silently won
    print(f"RUN {layout} {which} {out | {'ordinals': '...'}}")
    return out


def test_r0_one_change_through_append_holds_only_its_field_and_its_stamp(agent_record, tmp_path):
    """Shape: an appended change is a field tree with key and the one field it changed. The value and
    the stamp sit under that field, and the stamp names the writer. No other field's path exists."""
    repo = make_repo(tmp_path)
    message, event = change("field", "item-0", "Status", "In Progress")
    got = agent_record.append(repo, "a", event, org=ORG, message=message, schema=SCHEMA)
    assert got.ordinal == 1
    t = written_tree(repo, "a", got)
    assert paths(repo, t) == {KEY, f"{STATUS}/{VALUE}", f"{STATUS}/{FSTAMP}/{BY}",
                              f"{STATUS}/{FSTAMP}/002.2_ts", f"{STATUS}/{FSTAMP}/003.2_rheon"}
    assert git(repo, "cat-file", "blob", f"{t}:{STATUS}/{FSTAMP}/{BY}").stdout == "a"


def test_r1_field_stamp_different_fields_merge_clean_both_kept(agent_record, tmp_path):
    """R1 (<- itemstamp, stampall): stamp ON the field, A Status and B Desk: clean 100/100, both kept
    100/100."""
    got = run(agent_record, make_repo(tmp_path), "field", "diff")
    assert (got["clean"], got["both_kept"], got["conflicts"]) == (ROUNDS, ROUNDS, 0), got
    assert got["ordinals"] == {"a": list(range(1, ROUNDS + 1)), "b": list(range(1, ROUNDS + 1))}


def test_r2_field_stamp_same_field_a_named_conflict_never_picked(agent_record, tmp_path):
    """R2 (<- itemstamp, stampall, nostamp, append stamps nothing): stamp ON the field, A and B both
    Status: conflicts 100/100, named on Status/value and Status's stamp, and on nothing else; picked 0."""
    got = run(agent_record, make_repo(tmp_path), "field", "same")
    assert (got["conflicts"], got["picked"], got["clean"]) == (ROUNDS, 0, 0), got
    assert got["conflict_paths"].get(f"{STATUS}/{VALUE}") == ROUNDS, got
    assert got["conflict_paths"].get(f"{STATUS}/{FSTAMP}/{BY}") == ROUNDS, got
    assert all(p.startswith(f"{STATUS}/") for p in got["conflict_paths"]), got


def test_r3_item_stamp_different_fields_conflict_on_the_stamp_only(agent_record, tmp_path):
    """R3 (<- nostamp, append stamps nothing): stamp on the ITEM, A Status and B Desk: conflicts 100/100,
    and only on the stamp's path. The changed values themselves merge clean."""
    got = run(agent_record, make_repo(tmp_path), "item", "diff")
    assert (got["conflicts"], got["clean"]) == (ROUNDS, 0), got
    assert got["conflict_paths"] == {f"{ISTAMP}/{BY}": ROUNDS}, got


def test_r4_each_write_stamps_only_the_field_it_changed_with_its_own_name(agent_record, tmp_path):
    """R4 (<- itemstamp, stampall, nostamp, append stamps nothing): field layout, 200 writes. Each
    write's changed field carries its writer's own stamp (own_stamped 200), and no write carries any other
    field (200)."""
    got = run(agent_record, make_repo(tmp_path), "field", "diff")
    assert (got["own_stamped"], got["only_own_field"]) == (2 * ROUNDS, 2 * ROUNDS), got
