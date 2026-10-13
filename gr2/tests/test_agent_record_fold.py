"""The agent record's FOLD: the DAG-derived rows re-passed BY THE FUNCTION, with every write made by append.

THE APPEND it writes through, unchanged:

    append(repo, agent, event, ref="STOOK", *, message, schema=None, saw=()) -> Appended(commit, ordinal, retries_cas, retries_lock)

THE FOLD (same module, gr2/python_cli/agent_record.py):

    def fold(repo: Path, ref: str = "STOOK", *, message: str,
             schema: Schema | None = None) -> dict[tuple[str, str], str]

It reads every refs/<org>/__agents__/*/<ref> ONLY through ONE reused review_field_tree.BatchReader.
Each event is a board event under the caller's schema with string fields key, field, value, by (ts is
carried and NEVER read). The fold is DAG-DERIVED: for each (key, field), it keeps the writes that no
other write of that (key, field) descends from (commit ancestry; a commit that two refs reach is one
write). One kept is the value, one line ``"<key> <field> = <value> (by <by>)"``. More than one kept is a
named conflict ``"<key> <field> CONFLICT: <by>=<value> | <by>=<value>"``, with the sides sorted by
(by, value), a clock-free key. The fold never picks a side.

SAW across two sheaves: A appends to its own STOOK 'a'; B appends to its own STOOK 'b' with
saw=(A's new tip,), so B's write has parents [B's old tip, A's tip] and descends from A's. FORK is the same
two STOOKs with no saw. (Round 0's B write has no old tip, so its only parent is A's.)

Rows ported from the earlier plumbing harness, numbers kept: 100 rounds, keys
r000..r099, even rounds SAW, odd rounds FORK. B's clock is skewed by 1..3600 s (random.Random(2)),
earlier, then later for R3. The skew is applied to the event's ts AND the commit dates. R4 is new
(git spawns per fold counted). Mutants come with the build turn: swap (R1), ts (resolve a
conflict by ts: R2 and R3), logorder (sides in rev-list order: R3), swap (keep the ancestor: R1),
pertip (a BatchReader per ref: R4), perpair (an ancestry walk per pair of writes: R5),
pergroup (an ancestry walk per (key, field) group over the shared history: R7);
each reds exactly its own rows.
"""
from __future__ import annotations

import importlib
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"  # the caller's org namespace; gr2 ships none

ROUNDS = 100
BASE_TS = 1_791_800_000  # 2026-10-12 UTC, fixed

BOARD_PROTO = """syntax = "proto3";
package example.board.v1;

message BoardEvent {
  string key = 1;
  string field = 2;
  string value = 3;
  string by = 4;
  string ts = 5;  // carried, never read by the fold; a string because the field-tree writer refuses int64
}
"""
BOARD = fd.parse_proto(BOARD_PROTO)


@pytest.fixture
def agent_record():
    """Imported per test so each row goes RED on its own, naming the missing module."""
    return importlib.import_module("gr2.python_cli.agent_record")


@pytest.fixture
def git_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every git process started, through a git on PATH that logs its args and hands on."""
    real = shutil.which("git")
    bindir, log = tmp_path / "bin", tmp_path / "spawns.log"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{Path(real).parent}")

    def take() -> list[str]:
        lines = log.read_text().splitlines() if log.exists() else []
        log.write_text("")
        return lines
    return take


def make_repo(tmp_path: Path, name: str) -> Path:
    r = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "--ref-format=files", str(r)], check=True)
    return r


def write(agent_record, monkeypatch, repo, agent, by, key, value, ts, saw=()):
    """One board event by append; the commit dates carry the writer's (skewed) clock."""
    date = f"{ts} +0000"
    for k, v in {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                 "GIT_AUTHOR_NAME": by, "GIT_AUTHOR_EMAIL": f"{by}@example.invalid",
                 "GIT_COMMITTER_NAME": by, "GIT_COMMITTER_EMAIL": f"{by}@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    ev = {"key": key, "field": "Status", "value": value, "by": by, "ts": str(ts)}
    return agent_record.append(repo, agent, ev, org=ORG, message="BoardEvent", schema=BOARD, saw=saw)


def run(agent_record, monkeypatch, repo, skew_sign, rounds=ROUNDS):
    """The rounds, every write on its writer's own STOOK. SAW (even): A on 'a', then B on 'b' with saw=(A's tip,).
    FORK (odd): A on 'a', B on 'b', no saw."""
    rnd = random.Random(2)
    want = {}
    for i in range(rounds):
        key, ts_a = f"r{i:03d}", BASE_TS + 10 * i
        ts_b = ts_a + skew_sign * rnd.randint(1, 3600)
        if i % 2 == 0:
            a = write(agent_record, monkeypatch, repo, "a", "A", key, "Review", ts_a)
            write(agent_record, monkeypatch, repo, "b", "B", key, "Done", ts_b, saw=(a.commit,))
            want[key] = ("SAW", f"{key} Status = Done (by B)")
        else:
            write(agent_record, monkeypatch, repo, "a", "A", key, "Review", ts_a)
            write(agent_record, monkeypatch, repo, "b", "B", key, "Blocked", ts_b)
            want[key] = ("FORK", f"{key} Status CONFLICT: A=Review | B=Blocked")
    return want


def lines_by_key(folded):
    assert all(field == "Status" for _, field in folded), folded
    return {key: line for (key, _), line in folded.items()}


def sides(line):
    return set(line.split("CONFLICT: ", 1)[1].split(" | ")) if "CONFLICT: " in line else set()


def test_r1_saw_resolves_to_the_descendant_against_the_clock(agent_record, monkeypatch, tmp_path):
    """R1: B's write descends from A's while B's clock reads EARLIER: the descendant wins, 50/50."""
    repo = make_repo(tmp_path, "skew-earlier")
    want = run(agent_record, monkeypatch, repo, -1)
    got = lines_by_key(agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD))
    saw = [k for k, (case, _) in want.items() if case == "SAW"]
    assert sum(got[k] == want[k][1] for k in saw) == len(saw) == 50, {k: got.get(k) for k in saw[:3]}
    # the SAW crosses sheaves: 50 of B's commits on 'b' have a parent that is a commit on A's 'a'
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout
    on_a = set(git("rev-list", agent_record.refname("a", org=ORG)).split())
    b_rows = [line.split() for line in git("rev-list", "--parents", agent_record.refname("b", org=ORG)).splitlines()]
    assert sum(any(p in on_a for p in ps) for c, *ps in b_rows if c not in on_a) == 50


def test_r2_fork_is_a_named_conflict_never_picked(agent_record, monkeypatch, tmp_path):
    """R2: neither write descends from the other: a named CONFLICT with both sides, 50/50, 0 picked.
    It checks the SET of sides; their order is R3's claim."""
    repo = make_repo(tmp_path, "skew-earlier")
    want = run(agent_record, monkeypatch, repo, -1)
    got = lines_by_key(agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD))
    fork = [k for k, (case, _) in want.items() if case == "FORK"]
    assert len(fork) == 50
    assert sum(sides(got[k]) == {"A=Review", "B=Blocked"} for k in fork) == 50, {k: got.get(k) for k in fork[:3]}
    assert sum("CONFLICT" not in got[k] for k in fork) == 0


def test_r3_fold_lines_identical_under_opposite_skews(agent_record, monkeypatch, tmp_path):
    """R3, CLOCK-BLIND: the same writes with B's clock earlier, then later: fold lines identical 100/100."""
    early, late = make_repo(tmp_path, "skew-earlier"), make_repo(tmp_path, "skew-later")
    want = run(agent_record, monkeypatch, early, -1)
    run(agent_record, monkeypatch, late, +1)
    a = lines_by_key(agent_record.fold(early, org=ORG, message="BoardEvent", schema=BOARD))
    b = lines_by_key(agent_record.fold(late, org=ORG, message="BoardEvent", schema=BOARD))
    assert len(a) == len(b) == len(want) == ROUNDS
    assert sum(a[k] == b[k] for k in want) == ROUNDS, [(a[k], b[k]) for k in want if a[k] != b[k]][:3]


def test_r4_one_reused_batch_reader_spawns_do_not_grow_with_writes(agent_record, monkeypatch, tmp_path, git_spawns):
    """R4: the fold reads ONLY through ONE BatchReader, reused: one reader made, one cat-file --batch,
    and the git spawns per fold are counted and the same for 10 rounds as for 100 (same two STOOKs)."""
    made = []
    real_init = fd.GitReader.__init__

    def counting_init(self, *a, **kw):
        made.append(type(self).__name__)
        real_init(self, *a, **kw)
    monkeypatch.setattr(fd.GitReader, "__init__", counting_init)

    spawns = {}
    for rounds in (10, ROUNDS):
        repo = make_repo(tmp_path, f"r{rounds}")
        run(agent_record, monkeypatch, repo, -1, rounds)
        git_spawns()
        made.clear()
        agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD)
        log = git_spawns()
        assert made == ["BatchReader"], made
        assert sum("cat-file --batch" in line for line in log) == 1, log
        spawns[rounds] = len(log)
    print(f"git spawns per fold: {spawns}")
    assert spawns[10] == spawns[ROUNDS], spawns


class _Counted(dict):
    """The fold's commit graph, every parent lookup counted and stopped at a bound."""
    def __init__(self, data, visits, bound, what):
        super().__init__(data)
        self.visits, self.bound, self.what = visits, bound, what

    def get(self, k, default=None):
        self.visits[0] += 1
        assert self.visits[0] <= self.bound, f"{self.what}: passed {self.bound} parent lookups"
        return super().get(k, default)

    def __getitem__(self, k):
        return self.get(k)


def _count_graph(agent_record, monkeypatch, bound, what):
    """Wrap the graph where the fold reads it, so every parent lookup by any reader of it is counted."""
    visits, real = [0], agent_record._graph
    monkeypatch.setattr(agent_record, "_graph", lambda *a: _Counted(real(*a), visits, bound, what))
    return visits


def test_r5_ancestry_walk_visits_are_linear_in_writes(agent_record, monkeypatch, tmp_path):
    """R5, COST WITHOUT A CLOCK: FORK on ONE (key, field), N=800 writes, A and B alternating on their own STOOKs,
    no saw. Every parent lookup the fold makes is counted; it stays under 4N (the clock visits each commit once).
    A walk per pair of writes makes ~N^3 lookups (32 M at N=800); the row stops it at the bound."""
    n, bound = 800, 4 * 800
    repo = make_repo(tmp_path, "fork")
    for i in range(n):
        agent, by = ("a", "A") if i % 2 == 0 else ("b", "B")
        write(agent_record, monkeypatch, repo, agent, by, "k", f"v{i}", BASE_TS + i)

    calls, real = [0], agent_record._kept

    def counted_kept(*a):
        calls[0] += 1
        return real(*a)
    monkeypatch.setattr(agent_record, "_kept", counted_kept)
    visits = _count_graph(agent_record, monkeypatch, bound, f"ancestry walk for {n} writes")

    got = agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD)
    print(f"R5 ancestry walk: N={n} parent lookups={visits[0]} bound={bound} kept calls={calls[0]}")
    assert list(got) == [("k", "Status")], got  # what the line says is R2's and R3's claim, not this row's
    assert calls[0] == 1 and 0 < visits[0] <= bound, (calls[0], visits[0])


def reference_fold(agent_record, monkeypatch, repo, ref="STOOK"):
    """The fold with its kept writes found the old way, a walk per (key, field) group over the group's
    ancestors: slow and plainly right, the reference the clock's lines must equal."""
    with monkeypatch.context() as m:
        graph, real_graph = {}, agent_record._graph

        def keep_graph(*a):
            graph.update(real_graph(*a))
            return graph

        def walk_kept(writes, clock, at):
            seen, todo = set(), [p for w in writes for p in graph.get(w, ())]
            while todo:
                c = todo.pop()
                if c not in seen:
                    seen.add(c)
                    todo.extend(graph.get(c, ()))
            return [w for w in writes if w not in seen]
        m.setattr(agent_record, "_graph", keep_graph)
        m.setattr(agent_record, "_kept", walk_kept)
        return agent_record.fold(repo, ref, org=ORG, message="BoardEvent", schema=BOARD)


def test_r7_parent_lookups_stay_linear_across_many_key_field_groups(agent_record, monkeypatch, tmp_path):
    """R7, COST ACROSS GROUPS, which R5 bounds within one group and cannot see: N=400 writes over 100 keys
    (4 writes each: A, B, A, B; the first B always SAW the A before it, the last B SAW it in every other key,
    so half the keys resolve and half end in a conflict). Every group's writes sit in one shared history,
    so a walk per group makes about groups x history lookups (~40 k here, N^2 / 4); one clock per fold makes
    about 2N. Bounded at 4N, and the lines must equal those of the per-group walk (the reference) on the same
    writes, conflicts and SAW resolutions both present."""
    n, keys, bound = 400, 100, 4 * 400
    repo = make_repo(tmp_path, "groups")
    last_a = None
    for i in range(n):
        key = f"k{(i // 4) % keys:03d}"
        if i % 2 == 0:
            last_a = write(agent_record, monkeypatch, repo, "a", "A", key, f"v{i}", BASE_TS + i)
        else:
            saw = (last_a.commit,) if i % 4 == 1 or (i % 4 == 3 and (i // 4) % 2 == 0) else ()
            write(agent_record, monkeypatch, repo, "b", "B", key, f"v{i}", BASE_TS + i, saw=saw)
    want = reference_fold(agent_record, monkeypatch, repo)

    visits = _count_graph(agent_record, monkeypatch, bound, f"fold over {keys} groups and {n} writes")
    got = agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD)
    print(f"R7 groups: N={n} groups={keys} parent lookups={visits[0]} bound={bound}")
    assert got == want and len(got) == keys
    assert any("CONFLICT" in line for line in got.values()) and any("CONFLICT" not in line for line in got.values())
    assert 0 < visits[0] <= bound, visits[0]


def test_r8_a_saw_of_a_commit_no_folded_ref_holds_folds_like_the_reference(agent_record, monkeypatch, tmp_path):
    """R8: B's STOOK write SAW a commit on A's `other` ref, which the STOOK fold does not read. That commit is in the
    graph (B's write has it as its second parent; B's first is B's own earlier write) but on no folded chain. The fold neither fails nor invents a reach: its
    lines equal the reference's, and B's later write and A's STOOK write stay a conflict."""
    repo = make_repo(tmp_path, "other-ref")
    write(agent_record, monkeypatch, repo, "b", "B", "j", "first", BASE_TS - 1)  # so the SAW is B's SECOND parent
    write(agent_record, monkeypatch, repo, "a", "A", "k", "stook-a", BASE_TS)
    other = agent_record.append(repo, "a", {"key": "k", "field": "Status", "value": "other", "by": "A",
                                            "ts": str(BASE_TS + 1)}, "other", org=ORG, message="BoardEvent",
                                schema=BOARD)
    write(agent_record, monkeypatch, repo, "b", "B", "k", "stook-b", BASE_TS + 2, saw=(other.commit,))
    got = agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD)
    assert got == reference_fold(agent_record, monkeypatch, repo)
    assert sides(got[("k", "Status")]) == {"A=stook-a", "B=stook-b"}, got
    assert got[("j", "Status")] == "j Status = first (by B)", got


def case_folds(tmp_path: Path) -> bool:
    """True when this filesystem folds case (macOS by default): two names differing only in case are one file."""
    probe = tmp_path / "case-probe"
    probe.mkdir()
    (probe / "x").write_text("x")
    return (probe / "X").exists()


def test_r6_case_twins_are_refused_and_a_true_fork_still_folds_to_a_conflict(agent_record, monkeypatch, tmp_path):
    """Rule: an agent name is lowercase, so "Alice" is refused by name before any write. Why: on a filesystem
    that folds case, the files backend keeps "Alice" and "alice" as ONE loose ref, so alice's write would take
    Alice's tip as its first parent and the fold would read their fork as a write that saw the other, picking
    a side. The files backend is used explicitly (reftable does not fold case). Control: two distinct
    lowercase agents writing the same key with no saw still fold to a CONFLICT."""
    repo = make_repo(tmp_path, "case")
    with pytest.raises(ValueError):
        write(agent_record, monkeypatch, repo, "Alice", "Alice", "k", "Review", BASE_TS)
    refs = subprocess.run(["git", "-C", str(repo), "for-each-ref"], capture_output=True, text=True, check=True).stdout
    assert refs == "", "a refused name must leave no ref behind"
    write(agent_record, monkeypatch, repo, "alice", "alice", "k", "Review", BASE_TS)
    write(agent_record, monkeypatch, repo, "bob", "bob", "k", "Blocked", BASE_TS + 1)
    line = lines_by_key(agent_record.fold(repo, org=ORG, message="BoardEvent", schema=BOARD))["k"]
    assert sides(line) == {"alice=Review", "bob=Blocked"}, line
    if case_folds(tmp_path):  # where it matters: "ALICE" is the same file as "alice" here, and still refused
        with pytest.raises(ValueError):
            write(agent_record, monkeypatch, repo, "ALICE", "ALICE", "k", "Blocked", BASE_TS + 2)
