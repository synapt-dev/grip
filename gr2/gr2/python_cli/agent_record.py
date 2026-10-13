"""An agent's own record: an append-only ref of field-tree events.

Each agent writes to ``refs/<org>/__agents__/<agent>/<ref>``. The org namespace is the caller's
(``org=``); this module names no org. ``STOOK`` is the default ref: the canonical head of a
thing's record (like ``HEAD`` for a repository); other names beside it are branches.
The tip tree holds one entry per event, ``0000001..n`` (field tree ordinals, as
``review_field_tree`` names repeated occurrences). Each entry is the event's field tree
under the caller's schema.

``append`` is the only writer. The rules:

- One event = one commit. Its first parent is the ref's old tip. Its other parents are the
  tips the writer SAW, from any agent's ref (``saw=``), so a fold can derive "this write
  knew that one" from the DAG and not from a clock. Two writers that saw the same tip X and
  not each other are a concurrent pair whose merge-base is X.
- The ref moves by compare-and-swap (``update-ref <ref> <new> <old>``). Only the writer's own
  ref is guarded; a seen tip may move on without invalidating the write.
- On a refusal, a CAS (the old value moved) or a LOCK (another update holds the ref store),
  append re-reads the tip and rebuilds the tree from it before retrying, keeping ``saw``.
  Reusing a tree built before the refusal would drop the write that won.
- The loop is BOUNDED: after ``attempts`` refusals (``ATTEMPTS`` by default) append raises
  ``AppendRetriesExhausted`` naming the ref and the refusal counts, and the ref is unmoved. A
  lock left by a crashed writer (``<ref>.lock`` on files, ``tables.list.lock`` on reftable)
  refuses every attempt, so without a bound append would spin forever.
- append stamps what the event carries with ``agent``: every field that is a message with
  a ``stamp`` field, and the message's own ``stamp`` field if it has one. A write carries
  only the fields it changes, so only those fields are stamped. The stamp's other fields
  (ts, ...) are the caller's.

``fold`` is the reader. It is DAG-DERIVED: for each (key, field), it keeps the writes that no
other write of that (key, field) descends from (commit ancestry, never a clock). One kept is
the value; more than one is a named conflict with every side, sorted by (by, value), a
clock-free key. It never picks a side.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import gitops
from . import review_field_tree as fd
from .names import strict as _leaf


def prefix(org: str) -> str:
    """The ref space that holds every agent's refs in the caller's ``org`` namespace."""
    return f"refs/{_leaf(org, 'org')}/__agents__/"
ZERO = "0" * 40
STAMP = "stamp"
ATTEMPTS = 64  # update-ref attempts per append; each refusal is one retry, the last one raises


class AppendError(RuntimeError):
    pass


class AppendRetriesExhausted(AppendError):
    """update-ref refused every one of ``attempts`` tries; nothing moved the ref."""

    def __init__(self, ref: str, retries_cas: int, retries_lock: int):
        self.ref, self.retries_cas, self.retries_lock = ref, retries_cas, retries_lock
        super().__init__(f"update-ref {ref}: refused {retries_cas + retries_lock} times "
                         f"(cas {retries_cas}, lock {retries_lock}); gave up, ref unmoved")


class Appended(NamedTuple):
    commit: str        # the new tip of refs/<org>/__agents__/<agent>/<ref>
    ordinal: int       # this event's entry in the tip tree: f"{ordinal:07d}"
    retries_cas: int   # update-ref refused because the old value moved
    retries_lock: int  # update-ref refused on a lock


def refname(agent: str, ref: str = "STOOK", *, org: str) -> str:
    return f"{prefix(org)}{_leaf(agent, 'agent')}/{_leaf(ref, 'ref')}"


def _run(repo: Path, *args: str, data: bytes | None = None):
    return gitops.run(repo, *args, input=data, binary=True)


def _read_tip(repo: Path, name: str) -> str | None:
    proc = _run(repo, "rev-parse", "-q", "--verify", f"{name}^{{commit}}")
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def _stamped(event: dict, message: str, schema: fd.Schema | None, agent: str) -> dict:
    """A copy of ``event`` with ``by`` set to ``agent`` in every stamp it carries."""
    spec = fd._spec(schema, message)
    full = fd.schema() if schema is None else schema

    def has_by(type_name: str) -> bool:
        return type_name in full and any(f.name == "by" for f in full[type_name].values())

    out = dict(event)
    for f in spec.values():
        if not f.message or f.repeated:
            continue
        if f.name == STAMP and has_by(f.type):
            out[STAMP] = {**event.get(STAMP, {}), "by": agent}
        elif f.name in event and isinstance(event[f.name], dict):
            inner = next((g for g in full[f.type].values() if g.name == STAMP), None)
            if inner is not None and inner.message and has_by(inner.type):
                out[f.name] = {**event[f.name], STAMP: {**event[f.name].get(STAMP, {}), "by": agent}}
    return out


def _parents(old: str | None, saw: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for p in ([old] if old else []) + list(saw):
        if p not in out:  # git drops a repeated parent with a warning; say it once
            out.append(p)
    return out


def _build(repo: Path, old: str | None, leaf: str, saw: tuple[str, ...], message: str) -> tuple[str, int]:
    """A commit whose tree is ``old``'s entries plus the next ordinal holding ``leaf``."""
    entries = fd._ls(repo, f"{old}^{{tree}}") if old else []
    ordinal = len(entries) + 1
    tree = fd._mktree(repo, entries + [(fd._TREE_MODE, "tree", leaf, f"{ordinal:07d}")])
    argv = ["commit-tree", tree, "-m", f"{message} {ordinal:07d}"]
    for p in _parents(old, saw):
        argv += ["-p", p]
    proc = _run(repo, *argv)
    if proc.returncode != 0:
        raise AppendError(f"commit-tree failed: {proc.stderr.decode(errors='replace').strip()[:200]}")
    return proc.stdout.decode().strip(), ordinal


def append(repo: Path, agent: str, event: dict, ref: str = "STOOK", *, org: str, message: str,
           schema: fd.Schema | None = None, saw: tuple[str, ...] = (),
           attempts: int = ATTEMPTS) -> Appended:
    """Append ``event`` (a ``message`` under ``schema``) to the agent's ref; see the module doc."""
    repo, name, saw = Path(repo), refname(agent, ref, org=org), tuple(saw)
    leaf = fd.write_record(repo, _stamped(event, message, schema, agent), message, schema)
    cas = lock = 0
    while cas + lock < attempts:
        old = _read_tip(repo, name)
        commit, ordinal = _build(repo, old, leaf, saw, message)
        proc = _run(repo, "update-ref", name, commit, old or ZERO)
        if proc.returncode == 0:
            return Appended(commit, ordinal, cas, lock)
        err = proc.stderr.decode(errors="replace")
        if "expected" in err or "already exists" in err:
            cas += 1
        elif "cannot lock references" in err or "File exists" in err:
            lock += 1
        else:
            raise AppendError(f"update-ref {name} failed: {err.strip()[:200]}")
    raise AppendRetriesExhausted(name, cas, lock)


def _events(repo: Path, reader: fd.BatchReader, tips: dict[str, tuple[str, str]],
            first: dict[str, str], message: str, schema: fd.Schema | None) -> dict[str, dict]:
    """commit -> its event. A tip's tree holds entries 0000001..n; the commit with ordinal k is
    the tip's (n - k)th first parent. Only tip trees are listed, so ls-trees grow with refs, not writes."""
    out: dict[str, dict] = {}
    for tip, tree in tips.values():
        entries = {name: oid for _, kind, oid, name in reader.ls(tree) if kind == "tree"}
        commit, k = tip, len(entries)
        while k >= 1 and commit not in out:
            out[commit] = fd.read_record(repo, entries[f"{k:07d}"], message, schema, reader=reader)
            commit, k = first.get(commit), k - 1
    return out


def _kept(parents: dict[str, list[str]], writes: list[str]) -> list[str]:
    """The writes no other write descends from: one walk over the strict ancestors of all of them,
    so a (key, field) costs one pass over its history, not one per pair of writes."""
    seen, todo = set(), [p for w in writes for p in parents.get(w, ())]
    while todo:
        c = todo.pop()
        if c not in seen:
            seen.add(c)
            todo.extend(parents.get(c, ()))
    return [w for w in writes if w not in seen]

def fold(repo: Path, ref: str = "STOOK", *, org: str, message: str,
         schema: fd.Schema | None = None) -> dict[tuple[str, str], str]:
    """Every agent's ``ref`` folded to one line per (key, field); see the module doc.
    The event is a ``message`` under ``schema`` with string fields key, field, value and by.
    Lines: ``"<key> <field> = <value> (by <by>)"``, or
    ``"<key> <field> CONFLICT: <by>=<value> | <by>=<value>"``. Objects are read only through
    one BatchReader; the commit graph comes from one rev-list. Git spawns grow with refs, not writes."""
    repo = Path(repo)
    proc = _run(repo, "for-each-ref", "--format=%(objectname) %(tree)", f"{prefix(org)}*/{_leaf(ref, 'ref')}")
    if proc.returncode != 0:
        raise AppendError(f"for-each-ref failed: {proc.stderr.decode(errors='replace').strip()[:200]}")
    tips = {}
    for line in proc.stdout.decode().splitlines():
        commit, tree = line.split()
        tips[commit] = (commit, tree)
    if not tips:
        return {}
    proc = _run(repo, "rev-list", "--parents", "--stdin", data="".join(f"{c}\n" for c in tips).encode())
    if proc.returncode != 0:
        raise AppendError(f"rev-list failed: {proc.stderr.decode(errors='replace').strip()[:200]}")
    parents = {}
    for line in proc.stdout.decode().splitlines():
        c, *ps = line.split()
        parents[c] = ps
    first = {c: ps[0] for c, ps in parents.items() if ps}
    with fd.BatchReader(repo) as reader:
        events = _events(repo, reader, tips, first, message, schema)
    groups: dict[tuple[str, str], list[str]] = {}
    for commit, ev in events.items():
        groups.setdefault((ev["key"], ev["field"]), []).append(commit)
    out = {}
    for (key, field), writes in groups.items():
        kept = _kept(parents, writes)
        if len(kept) == 1:
            ev = events[kept[0]]
            out[(key, field)] = f"{key} {field} = {ev['value']} (by {ev['by']})"
        else:
            sides = sorted((events[w]["by"], events[w]["value"]) for w in kept)
            out[(key, field)] = f"{key} {field} CONFLICT: " + " | ".join(f"{b}={v}" for b, v in sides)
    return out
