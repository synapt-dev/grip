"""APPEND: an agent's own record, written by gr2.python_cli.agent_record.append.

    class Appended(NamedTuple):
        commit: str        # the new tip of refs/<org>/__agents__/<agent>/<ref>
        ordinal: int       # the field tree ordinal this event took: entry f"{ordinal:07d}" in the tip tree
        retries_cas: int   # update-ref refused because the old value moved (re-read, rebuilt, retried)
        retries_lock: int  # update-ref refused on a lock (reftable's one tables.list; re-read, rebuilt, retried)

    def append(repo: Path, agent: str, event: dict, ref: str = "STOOK", *,
               message: str, schema: Schema | None = None, saw: tuple[str, ...] = ()) -> Appended

It writes ``event`` with the field tree writer (``review_field_tree.write_record(repo, event, message,
schema)``) and commits a tip tree that is the re-read tip's entries plus the next ordinal, named
``0000001..n`` exactly as a field tree names repeated occurrences. Its parents are exactly [the previous tip, *saw]:
``saw`` names tips the writer READ, from any agent's ref. The ref moves by
``git update-ref <ref> <new> <old>`` (CAS; ZERO for a first write). On a CAS or LOCK refusal it re-reads
the tip and REBUILDS the tree from that tip before retrying, keeping saw. The loop is bounded: after
``attempts`` refusals (``ATTEMPTS``) it raises ``AppendRetriesExhausted(ref, retries_cas, retries_lock)``.

The concurrency rows: two writer processes, 100 barrier rounds, 200 writes, lock timeout 0.
R1/R2 own refs on files, R3 own refs on reftable, R4 one shared ref on both backends, R5 saw,
R6 a stale lock on both backends, R7 the attempts= bound, R8 the bound reached on CAS refusals.
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"  # the caller's org namespace; gr2 ships none

ROUNDS = 100
WRITERS = ("a", "b")
PREFIX = f"refs/{ORG}/__agents__/"

EVENT_PROTO = """syntax = "proto3";
package example.events.v1;

message Event {
  string by = 1;
  string seq = 2;  // slice 1 writes string/bytes/messages only (int64 refused, RAN)
  string ts = 3;
}
"""
EVENTS = fd.parse_proto(EVENT_PROTO)

ENV = dict(os.environ, GIT_AUTHOR_NAME="append-test", GIT_AUTHOR_EMAIL="append-test@example.invalid",
           GIT_COMMITTER_NAME="append-test", GIT_COMMITTER_EMAIL="append-test@example.invalid")


@pytest.fixture
def agent_record():
    """Imported per test so each row goes RED on its own, naming the missing module."""
    return importlib.import_module("gr2.python_cli.agent_record")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env=ENV).stdout.decode().strip()


def make_repo(tmp_path: Path, backend: str) -> Path:
    r = tmp_path / f"{backend}.git"
    subprocess.run(["git", "init", "-q", "--bare", f"--ref-format={backend}", str(r)], check=True)
    # lock timeout 0: a lock refusal surfaces to the verb instead of being waited out in git
    git(r, "config", "core.filesRefLockTimeout", "0")
    git(r, "config", "reftable.lockTimeout", "0")
    return r


def _writer(repo: str, agent: str, by: str, rounds: int, barrier, q) -> None:
    os.environ.update({k: v for k, v in ENV.items() if k.startswith("GIT_")})
    try:
        append = importlib.import_module("gr2.python_cli.agent_record").append
        tally = {"cas": 0, "lock": 0, "secs": [], "ordinals": []}
        for i in range(1, rounds + 1):
            barrier.wait(timeout=60)
            t = time.time()
            got = append(Path(repo), agent, {"by": by, "seq": str(i), "ts": str(int(t))}, org=ORG, message="Event", schema=EVENTS)
            tally["cas"] += got.retries_cas
            tally["lock"] += got.retries_lock
            tally["secs"].append(int(t))
            tally["ordinals"].append(got.ordinal)
        q.put((by, tally))
    except BaseException as e:  # noqa: BLE001 -- a writer must report, never hang the barrier
        barrier.abort()
        q.put((by, {"error": f"{type(e).__name__}: {e}"}))


def run(repo: Path, layout: str, rounds: int = ROUNDS) -> dict:
    """Two writer processes, one barrier per round. own: each on its own STOOK; shared: one STOOK."""
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(len(WRITERS)), ctx.Queue()
    agent = {w: (w if layout == "own" else "shared") for w in WRITERS}
    ps = [ctx.Process(target=_writer, args=(str(repo), agent[w], w, rounds, barrier, q)) for w in WRITERS]
    for p in ps:
        p.start()
    got = dict(q.get(timeout=600) for _ in ps)
    for p in ps:
        p.join(timeout=60)
    errors = {w: t["error"] for w, t in got.items() if "error" in t}
    assert not errors, errors

    seen, numbered_ok = [], True
    refs = git(repo, "for-each-ref", "--format=%(refname)", PREFIX).splitlines()
    for r in refs:
        entries = fd._ls(repo, git(repo, "rev-parse", f"{r}^{{tree}}"))
        names = [e[3] for e in entries]
        numbered_ok &= names == [f"{k:07d}" for k in range(1, len(names) + 1)]
        for _, kind, oid, _ in entries:
            assert kind == "tree", f"{r}: an ordinal must hold the event's field tree, got a {kind}"
            e = fd.read_record(repo, oid, "Event", EVENTS)
            seen.append((e["by"], int(e["seq"])))
    want = {(w, i) for w in WRITERS for i in range(1, rounds + 1)}
    got = {"writes": len(want), "lost": len(want - set(seen)), "extra": len(seen) - len(want & set(seen)),
            "numbered_ok": numbered_ok, "refs": len(refs),
            "retries_cas": sum(t["cas"] for t in got.values()),
            "retries_lock": sum(t["lock"] for t in got.values()),
            "same_second_rounds": sum(x == y for x, y in zip(got["a"]["secs"], got["b"]["secs"])),
            "ordinals": {w: t["ordinals"] for w, t in got.items()}}
    print("\nRUN", repo.name, layout, {k: v for k, v in got.items() if k != "ordinals"})
    return got


def test_r0_append_writes_the_next_ordinal_as_a_field_tree_under_the_callers_schema(agent_record, tmp_path):
    """Rule: one append = one commit on refs/<org>/__agents__/<agent>/<ref>, parent the old tip,
    tree = old entries + f"{ordinal:07d}" holding the event's field tree; ref defaults to STOOK."""
    repo = make_repo(tmp_path, "files")
    one = agent_record.append(repo, "ann", {"by": "ann", "seq": "1", "ts": "7"}, org=ORG, message="Event", schema=EVENTS)
    two = agent_record.append(repo, "ann", {"by": "ann", "seq": "2", "ts": "7"}, org=ORG, message="Event", schema=EVENTS)
    other = agent_record.append(repo, "ann", {"by": "ann", "seq": "3", "ts": "7"}, "other", org=ORG, message="Event", schema=EVENTS)
    assert (one.ordinal, two.ordinal, other.ordinal) == (1, 2, 1)
    assert git(repo, "rev-parse", f"{PREFIX}ann/STOOK") == two.commit
    assert git(repo, "rev-parse", f"{PREFIX}ann/other") == other.commit
    assert git(repo, "rev-parse", f"{two.commit}^@") == one.commit
    assert (one.retries_cas, one.retries_lock, two.retries_cas, two.retries_lock) == (0, 0, 0, 0)
    entries = fd._ls(repo, git(repo, "rev-parse", f"{two.commit}^{{tree}}"))
    assert [e[3] for e in entries] == ["0000001", "0000002"]
    assert entries[0][2] == git(repo, "rev-parse", f"{one.commit}^{{tree}}:0000001")  # old entries kept as-is
    assert fd.read_record(repo, entries[1][2], "Event", EVENTS) == {"by": "ann", "seq": "2", "ts": "7"}
    fd.verify_tree(repo, entries[1][2], "Event", EVENTS)


def test_r1_files_own_stooks_lost_0_numbered_intact_same_second(agent_record, tmp_path):
    """R1: files, own STOOKs: 200 writes, lost 0, NUMBERED 0000001..n, 100/100 rounds same second."""
    got = run(make_repo(tmp_path, "files"), "own")
    assert (got["writes"], got["lost"], got["extra"], got["refs"]) == (200, 0, 0, 2), got
    assert got["numbered_ok"], got
    assert got["ordinals"] == {w: list(range(1, ROUNDS + 1)) for w in WRITERS}
    assert got["same_second_rounds"] == ROUNDS, got


def test_r2_files_own_stooks_different_refs_never_contend(agent_record, tmp_path):
    """R2: files, own STOOKs, lock timeout 0: 0 retries of any kind, counted by the verb."""
    got = run(make_repo(tmp_path, "files"), "own")
    assert got["retries_cas"] + got["retries_lock"] == 0 and got["refs"] == 2, got


def test_r3_reftable_own_stooks_lost_0_lock_losers_retried(agent_record, tmp_path):
    """R3: reftable, own STOOKs, lock timeout 0: lost 0 (a writer that gives up loses writes here); refusals are
    LOCK, not CAS, since each ref has one writer. The count varies run to run (15-18 seen), so it is
    asserted as counted, not as a number."""
    got = run(make_repo(tmp_path, "reftable"), "own")
    assert (got["writes"], got["lost"], got["extra"], got["refs"]) == (200, 0, 0, 2), got
    assert got["numbered_ok"], got
    assert got["retries_cas"] == 0 and got["retries_lock"] >= 0, got


@pytest.mark.parametrize("backend", ["files", "reftable"])
def test_r4_one_shared_stook_cas_contended_lost_0(agent_record, tmp_path, backend):
    """R4, both backends: two writers on ONE STOOK: CAS contended (> 0 retries)
    and lost 0, the loser's tree REBUILT from the re-read tip: one dense 0000001..0000200."""
    got = run(make_repo(tmp_path, backend), "shared")
    assert (got["writes"], got["lost"], got["extra"], got["refs"]) == (200, 0, 0, 1), got
    assert got["numbered_ok"], got
    assert got["retries_cas"] > 0, got
    assert sorted(got["ordinals"]["a"] + got["ordinals"]["b"]) == list(range(1, 2 * ROUNDS + 1))


def test_r5_saw_tips_are_extra_parents_and_a_cas_retry_keeps_them(agent_record, tmp_path, monkeypatch):
    """R5: parents == [own old tip, *saw]; the CAS guards only the writer's own ref and a
    retry keeps saw. Two writers that both saw X (and not each other) are concurrent, merge-base X."""
    repo = make_repo(tmp_path, "files")
    ev = lambda by, i: {"by": by, "seq": str(i), "ts": "7"}
    ap = lambda agent, i, **kw: agent_record.append(repo, agent, ev(agent, i), org=ORG, message="Event", schema=EVENTS, **kw)
    x = ap("x", 1).commit
    b0 = ap("b", 1).commit
    b1 = ap("b", 2, saw=(x,))
    assert git(repo, "rev-parse", f"{b1.commit}^@").splitlines() == [b0, x]
    c1 = ap("c", 1, saw=(x,))  # first write on c: no old tip, so saw is every parent
    assert git(repo, "rev-parse", f"{c1.commit}^@").splitlines() == [x]
    assert git(repo, "merge-base", "--all", b1.commit, c1.commit) == x
    for t, u in ((b1.commit, c1.commit), (c1.commit, b1.commit)):
        assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", t, u]).returncode == 1

    # a CAS retry: a second writer lands on b's ref between b's read and its update-ref
    real, raced = agent_record._read_tip, []

    def racing(repo_, name):
        tip = real(repo_, name)
        if not raced and name.endswith("/b/STOOK"):
            raced.append(None)  # set first: the nested append reads through this hook too
            raced[0] = ap("b", 99).commit
        return tip

    monkeypatch.setattr(agent_record, "_read_tip", racing)
    b2 = ap("b", 3, saw=(x, c1.commit))
    assert (b2.retries_cas, b2.retries_lock, b2.ordinal) == (1, 0, 4), b2
    assert git(repo, "rev-parse", f"{b2.commit}^@").splitlines() == [raced[0], x, c1.commit]
    assert git(repo, "rev-parse", f"{PREFIX}b/STOOK") == b2.commit


class Hung(Exception):
    pass


@pytest.mark.parametrize("backend", ["files", "reftable"])
def test_r6_a_stale_lock_raises_the_named_error_within_the_bound_and_moves_nothing(agent_record, tmp_path, backend):
    """R6, both backends: a lock left by a crashed writer (files: <ref>.lock; reftable: tables.list.lock)
    refuses every update-ref. append raises AppendRetriesExhausted after ATTEMPTS lock refusals, naming
    the ref; the ref, its tree and the stale lock are as they were. The row bounds itself with an alarm,
    so an unbounded loop reds here instead of hanging. Once the lock is gone, the next append is ordinal 2."""
    repo = make_repo(tmp_path, backend)
    name = f"{PREFIX}a/STOOK"
    ev = lambda i: {"by": "a", "seq": str(i), "ts": "7"}
    one = agent_record.append(repo, "a", ev(1), org=ORG, message="Event", schema=EVENTS)
    lock = repo / ("reftable/tables.list.lock" if backend == "reftable" else f"{name}.lock")
    lock.write_bytes(b"left by a crashed writer\n")

    def hung(*_):
        raise Hung(f"append still retrying after {SECS} s: the loop is not bounded")

    SECS = 30
    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(SECS)
    t = time.monotonic()
    try:
        with pytest.raises(agent_record.AppendRetriesExhausted) as e:
            agent_record.append(repo, "a", ev(2), org=ORG, message="Event", schema=EVENTS)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    secs = time.monotonic() - t
    print(f"\nRUN {backend} stale lock: {type(e.value).__name__} in {secs:.2f} s: {e.value}")
    assert isinstance(e.value, agent_record.AppendError)
    assert (e.value.ref, e.value.retries_cas, e.value.retries_lock) == (name, 0, agent_record.ATTEMPTS)
    assert name in str(e.value) and f"lock {agent_record.ATTEMPTS}" in str(e.value)
    assert git(repo, "rev-parse", name) == one.commit  # unmoved
    assert [x[3] for x in fd._ls(repo, git(repo, "rev-parse", f"{name}^{{tree}}"))] == ["0000001"]
    assert lock.read_bytes() == b"left by a crashed writer\n"  # not ours to break

    lock.unlink()
    two = agent_record.append(repo, "a", ev(2), org=ORG, message="Event", schema=EVENTS)
    assert (two.ordinal, two.retries_cas, two.retries_lock) == (2, 0, 0), two
    assert git(repo, "rev-parse", f"{two.commit}^@") == one.commit


BAD_NAMES = [  # (org, agent, ref): each puts a caller's name somewhere other than one ref component
    ("example", "team/x", "STOOK"),   # nests below agent "team"; the fold's * would never see it
    ("example", "team", "x/STOOK"),   # the same ref from the other side: aliases with the row above
    ("example", "", "STOOK"),
    ("example", "a", ""),
    ("ex/ample", "a", "STOOK"),
    ("", "a", "STOOK"),
    ("example", "Alice", "STOOK"),    # uppercase: a case-insensitive filesystem folds it into "alice"
    ("Example", "a", "STOOK"),
    ("example", "a", "stook"),        # the default ref's lowercase twin: one file with STOOK there
    ("example", "a", "Other"),
    ("example", "a b", "STOOK"),      # git-invalid names fail HERE, not at update-ref after a write
    ("example", "a..b", "STOOK"),
    ("example", "x.lock", "STOOK"),
    ("example", ".hidden", "STOOK"),
    ("example", "-x", "STOOK"),
    ("example", "a.", "STOOK"),       # a trailing "." passed the class; git refused it only at update-ref
    ("example", "a", "a."),           #   after the objects were written
    # one case per remaining git check-ref-format rule, each refused by the character class
    ("example", "@", "STOOK"),        # the single "@"
    ("example", "a@{b", "STOOK"),     # "@{"
    ("example", "a\\b", "STOOK"),     # backslash
    ("example", "a\tb", "STOOK"),     # a control character
    ("example", "a~b", "STOOK"),
    ("example", "a^b", "STOOK"),
    ("example", "a:b", "STOOK"),
    ("example", "a?b", "STOOK"),
    ("example", "a*b", "STOOK"),
    ("example", "a[b", "STOOK"),
]


@pytest.mark.parametrize("org,agent,ref", BAD_NAMES)
def test_r11_a_name_that_is_not_one_ref_component_is_refused_before_anything_is_written(agent_record, tmp_path,
                                                                                       org, agent, ref):
    """Rule: org, agent and ref each sit as exactly one ref component; anything else raises
    ValueError before append writes an object, and fold refuses the same org and ref."""
    repo = make_repo(tmp_path, "files")
    objects = lambda: git(repo, "cat-file", "--batch-all-objects", "--batch-check")
    before = objects()
    with pytest.raises(ValueError):
        agent_record.append(repo, agent, {"by": "x", "seq": "1", "ts": "7"}, ref, org=org, message="Event",
                            schema=EVENTS)
    assert git(repo, "for-each-ref") == "" and objects() == before
    if org != "example" or ref != "STOOK":  # fold takes org and ref, not an agent
        with pytest.raises(ValueError):
            agent_record.fold(repo, ref, org=org, message="Event", schema=EVENTS)


def alarmed(fn, secs=30):
    """Run fn under an alarm, so an unbounded retry loop reds the row instead of hanging it."""
    def hung(*_):
        raise Hung(f"append still retrying after {secs} s: the loop is not bounded")
    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(secs)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("backend", ["files", "reftable"])
def test_r7_attempts_overrides_the_bound_per_call(agent_record, tmp_path, backend):
    """R7, both backends: attempts= is the bound for that call. Under a stale lock, attempts=3 raises
    after exactly 3 lock refusals, not ATTEMPTS; the ref is unmoved."""
    repo = make_repo(tmp_path, backend)
    name = f"{PREFIX}a/STOOK"
    one = agent_record.append(repo, "a", {"by": "a", "seq": "1", "ts": "7"}, org=ORG, message="Event", schema=EVENTS)
    lock = repo / ("reftable/tables.list.lock" if backend == "reftable" else f"{name}.lock")
    lock.write_bytes(b"left by a crashed writer\n")
    with pytest.raises(agent_record.AppendRetriesExhausted) as e:
        alarmed(lambda: agent_record.append(repo, "a", {"by": "a", "seq": "2", "ts": "7"}, org=ORG, message="Event",
                                            schema=EVENTS, attempts=3))
    print(f"\nRUN {backend} attempts=3: {e.value}")
    assert (e.value.ref, e.value.retries_cas, e.value.retries_lock) == (name, 0, 3)
    assert git(repo, "rev-parse", name) == one.commit


def test_r8_cas_refusals_exhaust_the_bound_and_are_counted_as_cas(agent_record, tmp_path, monkeypatch):
    """R8: a writer whose every read is stale (the tip moved after it and stays moved) is refused by the
    CAS on every try. append raises after ATTEMPTS CAS refusals, retries_cas == ATTEMPTS and lock 0;
    the ref stays where the other writer put it."""
    repo = make_repo(tmp_path, "files")
    name = f"{PREFIX}a/STOOK"
    ap = lambda i, **kw: agent_record.append(repo, "a", {"by": "a", "seq": str(i), "ts": "7"},
                                             org=ORG, message="Event", schema=EVENTS, **kw)
    one, two = ap(1).commit, ap(2).commit
    monkeypatch.setattr(agent_record, "_read_tip", lambda repo_, name_: one)  # always one behind
    with pytest.raises(agent_record.AppendRetriesExhausted) as e:
        alarmed(lambda: ap(3))
    print(f"\nRUN files stale read: {e.value}")
    assert (e.value.ref, e.value.retries_cas, e.value.retries_lock) == (name, agent_record.ATTEMPTS, 0)
    assert f"cas {agent_record.ATTEMPTS}" in str(e.value)
    assert git(repo, "rev-parse", name) == two
