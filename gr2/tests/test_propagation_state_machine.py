"""Prototype 0: the propagation state machine, proven on synthetic git repositories.

Everything here runs against throwaway repositories under ``tmp_path``. No real
workspace, remote, or authoring clone is touched. The synthetic topology is:

* one bare "source" remote with a config-like file on ``main``
* three destinations: a clean managed replica, a dirty authoring clone, and an
  authoring clone that is ahead of (and behind) the source

What the tests prove, each as its own witness:

* one change walks observed -> fetched -> planned -> applied -> verified ->
  acknowledged, and every receipt names exact source and destination revisions
* killing the sink between each pair of states and replaying applies the change
  exactly once, with the cursor advancing only on acknowledged
* a moved expected base refuses (compare-and-swap) and the receipt carries the
  observed base; nothing is merged or forced
* a dirty authoring clone and a diverged authoring clone are refused and left
  byte-for-byte untouched
* a destination that cannot be read back after the apply verb is recorded as
  ``unverifiable`` (never collapsed into refused or applied) and resolves on replay
* some destinations verifying while others refuse is ``partial``, both sides
  enumerated
* the born-red witness for the outbox: a consumer that fails after reading an
  event loses that event for good, because the cursor advances before the effect
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from gr2.prototypes.propagation_state_machine import (
    KILL_AFTER_APPLY_VERB,
    Coordinate,
    Destination,
    DestinationKind,
    Direction,
    Journal,
    JournalInconsistent,
    Operation,
    Policy,
    Propagator,
    SinkKilled,
    State,
    tree_digest,
)
from gr2.python_cli.events import EventType, emit, read_events

_SHA = re.compile(r"^[0-9a-f]{40}$")

# Commits in the synthetic repositories must not depend on, or touch, the
# machine's global git configuration (signing keys, hooks paths, identities).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "prototype",
    "GIT_AUTHOR_EMAIL": "prototype@example.invalid",
    "GIT_COMMITTER_NAME": "prototype",
    "GIT_COMMITTER_EMAIL": "prototype@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return proc.stdout.strip()


def head_moves(repo: Path) -> int:
    """How many times HEAD has moved since the clone (reflog entries minus the clone)."""
    lines = git(repo, "reflog", "show", "--format=%gs", "HEAD").splitlines()
    return len(lines) - 1


def snapshot(repo: Path) -> dict[str, str]:
    """Everything an 'untouched' claim is about: refs, HEAD, index+worktree state, bytes."""
    return {
        "refs": git(repo, "for-each-ref"),
        "head": git(repo, "rev-parse", "HEAD"),
        "porcelain": git(repo, "status", "--porcelain"),
        "canon": (repo / "canon.md").read_text(),
        "reflog": git(repo, "reflog", "show", "--format=%gs", "HEAD"),
    }


@dataclass
class Synthetic:
    remote: Path
    author: Path
    base: str
    state_dir: Path
    root: Path

    def push_change(self, text: str) -> str:
        (self.author / "canon.md").write_text(text)
        git(self.author, "add", "canon.md")
        git(self.author, "commit", "-q", "-m", f"canon: {text.strip()[:40]}")
        git(self.author, "push", "-q", "origin", "main")
        return git(self.author, "rev-parse", "HEAD")

    def clone(self, name: str) -> Path:
        path = self.root / name
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(path)],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **_GIT_ENV},
        )
        return path


@pytest.fixture
def synthetic(tmp_path: Path) -> Synthetic:
    remote = tmp_path / "source.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    author = tmp_path / "author"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(author)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    git(author, "switch", "-q", "-c", "main")
    (author / "canon.md").write_text("canon v1\n")
    git(author, "add", "canon.md")
    git(author, "commit", "-q", "-m", "canon v1")
    git(author, "push", "-q", "-u", "origin", "main")
    base = git(author, "rev-parse", "HEAD")
    state_dir = tmp_path / "sink-state"
    return Synthetic(remote=remote, author=author, base=base, state_dir=state_dir, root=tmp_path)


POLICY = Policy(policy_hash="policy-prototype-0", allowed_directions=frozenset({Direction.DOWN}))


def coordinate(destination_id: str, direction: Direction = Direction.DOWN) -> Coordinate:
    return Coordinate(
        source="source-0",
        destination=destination_id,
        layer="layer-0",
        direction=direction,
        operation=Operation.APPLY,
        artifact_class="class-0",
    )


def replica(syn: Synthetic, name: str = "replica") -> Destination:
    return Destination(destination_id=name, path=syn.clone(name), kind=DestinationKind.REPLICA)


def authoring(syn: Synthetic, name: str = "authoring") -> Destination:
    return Destination(destination_id=name, path=syn.clone(name), kind=DestinationKind.AUTHORING)


def propagator(syn: Synthetic, **kwargs) -> Propagator:
    return Propagator(
        source_remote=syn.remote, branch="main", state_dir=syn.state_dir, policy=POLICY, **kwargs
    )


# --------------------------------------------------------------------------- happy path


def test_one_change_reaches_acknowledged_and_names_exact_revisions(synthetic: Synthetic) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None
    assert receipt.state is State.ACKNOWLEDGED
    assert [t.state for t in receipt.transitions] == [
        State.OBSERVED,
        State.FETCHED,
        State.PLANNED,
        State.APPLIED,
        State.VERIFIED,
        State.ACKNOWLEDGED,
    ]
    # exact revisions, never prefixes
    assert receipt.source_rev == new and _SHA.match(receipt.source_rev)
    assert receipt.expected_base == synthetic.base
    assert receipt.observed_base == synthetic.base
    assert receipt.after == new
    assert receipt.operation_id is not None and len(receipt.operation_id) == 64
    assert receipt.digest == git(dest.path, "rev-parse", f"{new}^{{tree}}")
    # the destination really moved, read back from the destination itself
    assert git(dest.path, "rev-parse", "HEAD") == new
    assert (dest.path / "canon.md").read_text() == "canon v2\n"
    assert head_moves(dest.path) == 1
    # gate results are individual, each with a hash; the postcondition is named
    assert {g.gate_id for g in receipt.gate_results} == {
        "policy.direction",
        "destination.base-unmoved",
        "destination.clean",
        "destination.fast-forward",
    }
    assert all(g.result == "pass" and len(g.result_hash) == 64 for g in receipt.gate_results)
    assert receipt.postcondition_checked == "head-is-intended-after-and-tree-matches-digest"
    assert receipt.postcondition_holds is True
    assert receipt.refusal_reason is None
    # the cursor advanced, and only because acknowledged was reached
    assert Journal(synthetic.state_dir).cursor(coord.key()) == new
    # the receipt is a plain, JSON-serialisable record
    json.dumps(receipt.as_dict())


def test_no_new_source_revision_is_not_an_operation(synthetic: Synthetic) -> None:
    dest = replica(synthetic)
    synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    sink = propagator(synthetic)
    assert sink.run(coord, dest) is not None
    journal_lines = Journal(synthetic.state_dir).path.read_text()
    before = snapshot(dest.path)

    assert sink.run(coord, dest) is None

    assert snapshot(dest.path) == before
    assert Journal(synthetic.state_dir).path.read_text() == journal_lines


def test_observe_control_rereads_at_a_known_revision_and_reports_nothing(
    synthetic: Synthetic,
) -> None:
    new = synthetic.push_change("canon v2\n")
    sink = propagator(synthetic)
    fresh = sink.observe_source_at(cursor=None)
    assert fresh.source_rev == new and fresh.is_new is True
    # the control: the same read, at a cursor that already names the revision
    assert sink.observe_source_at(cursor=new).is_new is False
    # and at a cursor known to predate it, the change IS reported
    assert sink.observe_source_at(cursor=synthetic.base).is_new is True


def test_fetched_digest_control_known_different_object_mismatches(synthetic: Synthetic) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    receipt = propagator(synthetic).run(coordinate(dest.destination_id), dest)
    assert receipt is not None and receipt.digest is not None
    mirror = synthetic.state_dir / "mirror.git"
    assert tree_digest(mirror, new) == receipt.digest
    # the discriminating control: a known-different object recomputes to a different digest
    assert tree_digest(mirror, synthetic.base) != receipt.digest


# ------------------------------------------------------------------ kill + replay


@pytest.mark.parametrize(
    "kill_after",
    [
        State.OBSERVED,
        State.FETCHED,
        State.PLANNED,
        KILL_AFTER_APPLY_VERB,
        State.APPLIED,
        State.VERIFIED,
    ],
)
def test_kill_between_states_then_replay_applies_exactly_once(
    synthetic: Synthetic, kill_after
) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)

    with pytest.raises(SinkKilled):
        propagator(synthetic, kill_after=kill_after).run(coord, dest)

    journal = Journal(synthetic.state_dir)
    attempts = journal.find(coord.key(), new)
    assert len(attempts) == 1
    recorded = [t.state for t in attempts[0]]
    expected_last = State.PLANNED if kill_after == KILL_AFTER_APPLY_VERB else kill_after
    assert recorded[-1] is expected_last
    # the cursor never advances before acknowledged, whatever state the sink died in
    assert journal.cursor(coord.key()) is None
    verb_ran_before_kill = kill_after in (KILL_AFTER_APPLY_VERB, State.APPLIED, State.VERIFIED)
    assert git(dest.path, "rev-parse", "HEAD") == (new if verb_ran_before_kill else synthetic.base)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None and receipt.state is State.ACKNOWLEDGED
    assert receipt.replayed is False  # it was resumed, not a terminal no-op
    assert receipt.after == new and git(dest.path, "rev-parse", "HEAD") == new
    assert journal.cursor(coord.key()) == new
    # exactly one apply, measured two ways: the destination's own reflog and the journal
    assert head_moves(dest.path) == 1
    assert journal.notes(coord.key(), new, "apply-verb-started") == 1
    # one attempt, states strictly in order, no state skipped
    (transitions,) = journal.find(coord.key(), new)
    states = [t.state for t in transitions]
    assert states == [
        State.OBSERVED,
        State.FETCHED,
        State.PLANNED,
        State.APPLIED,
        State.VERIFIED,
        State.ACKNOWLEDGED,
    ]


def test_replay_of_an_acknowledged_operation_is_a_no_op_returning_the_original_outcome(
    synthetic: Synthetic,
) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    first = propagator(synthetic).run(coord, dest)
    assert first is not None and first.state is State.ACKNOWLEDGED
    # lose the cursor store (the kind of thing that happens); the journal still knows
    cursor_file = Journal(synthetic.state_dir).cursor_path(coord.key())
    cursor_file.unlink()
    before = snapshot(dest.path)

    again = propagator(synthetic).run(coord, dest)

    assert again is not None
    assert again.replayed is True
    assert again.state is State.ACKNOWLEDGED
    assert again.operation_id == first.operation_id
    assert again.after == first.after == new
    assert snapshot(dest.path) == before  # no verb ran against the destination
    assert head_moves(dest.path) == 1


def test_kill_after_acknowledged_row_before_cursor_replays_as_terminal_and_repairs_cursor(
    synthetic: Synthetic,
) -> None:
    # the acknowledged row is written before the cursor advances, so the journal is
    # the source of truth and the cursor is derived from it; a death in between must
    # not re-run anything and must leave the cursor repaired afterwards
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    with pytest.raises(SinkKilled):
        propagator(synthetic, kill_after=State.ACKNOWLEDGED).run(coord, dest)
    journal = Journal(synthetic.state_dir)
    (transitions,) = journal.find(coord.key(), new)
    assert transitions[-1].state is State.ACKNOWLEDGED
    assert journal.cursor(coord.key()) is None
    before = snapshot(dest.path)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None and receipt.replayed is True
    assert receipt.state is State.ACKNOWLEDGED
    assert journal.cursor(coord.key()) == new
    assert snapshot(dest.path) == before
    assert head_moves(dest.path) == 1
    # and now the source has nothing new for this coordinate
    assert propagator(synthetic).run(coord, dest) is None


# ---------------------------------------------------------------- refusals


def test_moved_expected_base_refuses_and_reports_observed_base(synthetic: Synthetic) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    with pytest.raises(SinkKilled):
        propagator(synthetic, kill_after=State.PLANNED).run(coord, dest)
    # between plan and apply, the destination moves
    (dest.path / "canon.md").write_text("local edit committed\n")
    git(dest.path, "commit", "-q", "-am", "local")
    moved = git(dest.path, "rev-parse", "HEAD")
    assert moved != synthetic.base

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None
    assert receipt.state is State.REFUSED
    assert receipt.expected_base == synthetic.base
    assert receipt.observed_base == moved
    assert receipt.refusal_reason is not None and "expected_base" in receipt.refusal_reason
    assert receipt.after is None
    # not merged, not forced, not retried against the new base
    assert git(dest.path, "rev-parse", "HEAD") == moved
    assert (dest.path / "canon.md").read_text() == "local edit committed\n"
    assert Journal(synthetic.state_dir).cursor(coord.key()) is None
    assert Journal(synthetic.state_dir).notes(coord.key(), new, "apply-verb-started") == 0


def test_dirty_authoring_clone_is_refused_and_untouched(synthetic: Synthetic) -> None:
    dest = authoring(synthetic)
    (dest.path / "canon.md").write_text("uncommitted authoring in progress\n")
    (dest.path / "scratch.txt").write_text("untracked\n")
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    before = snapshot(dest.path)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None and receipt.state is State.REFUSED
    assert receipt.source_rev == new and receipt.expected_base == synthetic.base
    failed = {g.gate_id: g for g in receipt.gate_results if g.result == "fail"}
    assert "destination.clean" in failed
    assert receipt.refusal_reason is not None and receipt.refusal_reason.startswith(
        "destination.clean"
    )
    # gates are recorded individually, including the ones that passed
    assert {g.gate_id for g in receipt.gate_results} >= {"policy.direction", "destination.clean"}
    # untouched means untouched: refs, HEAD, index/worktree, bytes, reflog
    assert snapshot(dest.path) == before
    assert (dest.path / "scratch.txt").read_text() == "untracked\n"


def test_diverged_authoring_clone_is_refused_with_counts_and_untouched(
    synthetic: Synthetic,
) -> None:
    dest = authoring(synthetic)
    (dest.path / "canon.md").write_text("local canon branch\n")
    git(dest.path, "commit", "-q", "-am", "local authoring commit")
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    before = snapshot(dest.path)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None and receipt.state is State.REFUSED
    failed = {g.gate_id: g for g in receipt.gate_results if g.result == "fail"}
    assert "destination.fast-forward" in failed
    assert "ahead=1" in failed["destination.fast-forward"].detail
    assert "behind=1" in failed["destination.fast-forward"].detail
    assert receipt.source_rev == new
    assert receipt.observed_base == before["head"]
    assert snapshot(dest.path) == before


def test_direction_outside_policy_is_refused_at_plan_and_destination_untouched(
    synthetic: Synthetic,
) -> None:
    dest = replica(synthetic)
    synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id, direction=Direction.UP)
    before = snapshot(dest.path)

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None and receipt.state is State.REFUSED
    assert [t.state for t in receipt.transitions][-2:] == [State.PLANNED, State.REFUSED]
    assert receipt.refusal_reason is not None and receipt.refusal_reason.startswith(
        "policy.direction"
    )
    assert snapshot(dest.path) == before


def test_refused_then_cleaned_is_a_new_attempt_not_a_replayed_refusal(synthetic: Synthetic) -> None:
    dest = authoring(synthetic)
    (dest.path / "canon.md").write_text("uncommitted\n")
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    first = propagator(synthetic).run(coord, dest)
    assert first is not None and first.state is State.REFUSED
    # the author cleans up; the same source revision at the same base is now applicable
    git(dest.path, "checkout", "--", "canon.md")

    second = propagator(synthetic).run(coord, dest)

    assert second is not None and second.state is State.ACKNOWLEDGED
    assert second.replayed is False
    assert second.after == new
    attempts = Journal(synthetic.state_dir).find(coord.key(), new)
    assert len(attempts) == 2
    assert [t.state for t in attempts[0]][-1] is State.REFUSED
    assert [t.state for t in attempts[1]][-1] is State.ACKNOWLEDGED


# ---------------------------------------------------------- verification + unverifiable


def test_verified_postcondition_mutation_fails_the_check_and_blocks_acknowledgement(
    synthetic: Synthetic,
) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    with pytest.raises(SinkKilled):
        propagator(synthetic, kill_after=State.APPLIED).run(coord, dest)
    assert git(dest.path, "rev-parse", "HEAD") == new
    # mutate the postcondition: the worktree no longer matches the applied tree
    (dest.path / "canon.md").write_text("mutated after apply\n")

    receipt = propagator(synthetic).run(coord, dest)

    assert receipt is not None
    assert receipt.state is State.APPLIED  # the highest state actually established
    assert receipt.postcondition_checked == "head-is-intended-after-and-tree-matches-digest"
    assert receipt.postcondition_holds is False
    assert Journal(synthetic.state_dir).cursor(coord.key()) is None
    assert [t.state for t in receipt.transitions][-1] is State.APPLIED
    # the control: undo the mutation and the same check verifies
    (dest.path / "canon.md").write_text("canon v2\n")
    control = propagator(synthetic).run(coord, dest)
    assert control is not None and control.state is State.ACKNOWLEDGED
    assert control.postcondition_holds is True


def test_unreadable_destination_after_apply_is_unverifiable_and_resolves_on_replay(
    synthetic: Synthetic,
) -> None:
    dest = replica(synthetic)
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    hidden = dest.path.with_name("replica-hidden")

    def hide_destination() -> None:
        dest.path.rename(hidden)

    receipt = propagator(synthetic, after_apply_verb=hide_destination).run(coord, dest)

    assert receipt is not None
    assert receipt.state is State.UNVERIFIABLE
    assert receipt.refusal_reason is None  # not refused
    assert receipt.after is None  # not applied either; nobody read it back
    assert "read back" in receipt.detail
    assert Journal(synthetic.state_dir).cursor(coord.key()) is None
    assert [t.state for t in receipt.transitions][-2:] == [State.PLANNED, State.UNVERIFIABLE]

    hidden.rename(dest.path)
    resolved = propagator(synthetic).run(coord, dest)

    assert resolved is not None and resolved.state is State.ACKNOWLEDGED
    assert resolved.after == new
    assert head_moves(dest.path) == 1
    assert Journal(synthetic.state_dir).notes(coord.key(), new, "apply-verb-started") == 1
    assert [t.state for t in resolved.transitions] == [
        State.OBSERVED,
        State.FETCHED,
        State.PLANNED,
        State.UNVERIFIABLE,
        State.APPLIED,
        State.VERIFIED,
        State.ACKNOWLEDGED,
    ]


# ------------------------------------------------------------------- partial


def test_mixed_destinations_are_partial_with_both_sides_enumerated(synthetic: Synthetic) -> None:
    clean = replica(synthetic, "replica")
    dirty = authoring(synthetic, "dirty-authoring")
    (dirty.path / "canon.md").write_text("uncommitted\n")
    ahead = authoring(synthetic, "ahead-authoring")
    (ahead.path / "canon.md").write_text("local\n")
    git(ahead.path, "commit", "-q", "-am", "local")
    new = synthetic.push_change("canon v2\n")
    targets = [(coordinate(d.destination_id), d) for d in (clean, dirty, ahead)]

    outcome = propagator(synthetic).run_all(targets)

    assert outcome.state is State.PARTIAL
    assert outcome.reached == ("replica",)
    assert {d for d, _reason in outcome.not_reached} == {"dirty-authoring", "ahead-authoring"}
    reasons = dict(outcome.not_reached)
    assert reasons["dirty-authoring"].startswith("destination.clean")
    assert reasons["ahead-authoring"].startswith("destination.fast-forward")
    # every receipt, reached or not, names exact revisions
    for receipt in outcome.receipts:
        assert _SHA.match(receipt.source_rev) and receipt.source_rev == new
        assert _SHA.match(receipt.expected_base)
        assert receipt.observed_base is not None and _SHA.match(receipt.observed_base)
    assert git(clean.path, "rev-parse", "HEAD") == new
    assert git(dirty.path, "rev-parse", "HEAD") == synthetic.base
    assert (dirty.path / "canon.md").read_text() == "uncommitted\n"


def test_replaying_a_partial_outcome_keeps_the_reached_target_and_stays_partial(
    synthetic: Synthetic,
) -> None:
    # Found in review: the first cut's run_all dropped a declared target whose cursor
    # was already at the source revision (run() returns None for "nothing new"), so a
    # replayed partial reclassified itself as REFUSED with reached=() -- the
    # aggregate could not tell "already reached" from "not part of this invocation".
    clean = replica(synthetic, "replica")
    dirty = authoring(synthetic, "dirty-authoring")
    (dirty.path / "canon.md").write_text("uncommitted\n")
    new = synthetic.push_change("canon v2\n")
    targets = [(coordinate(d.destination_id), d) for d in (clean, dirty)]

    first = propagator(synthetic).run_all(targets)
    assert first is not None and first.state is State.PARTIAL
    assert first.reached == ("replica",)
    assert [d for d, _ in first.not_reached] == ["dirty-authoring"]
    assert head_moves(clean.path) == 1

    # Replay with nothing changed: the SAME classification, the reached target is
    # still enumerated (as a replayed terminal receipt), and it is not applied again.
    second = propagator(synthetic).run_all(targets)
    assert second is not None and second.state is State.PARTIAL, second
    assert second.reached == ("replica",)
    assert [d for d, _ in second.not_reached] == ["dirty-authoring"]
    by_dest = {r.coordinate.destination: r for r in second.receipts}
    assert by_dest["replica"].replayed is True
    assert by_dest["replica"].state is State.ACKNOWLEDGED
    assert by_dest["replica"].after == new
    assert by_dest["dirty-authoring"].replayed is False  # a refusal starts a new attempt
    assert by_dest["dirty-authoring"].attempt == 2
    assert head_moves(clean.path) == 1, "the reached target must not be applied twice"

    # The author cleans up: the refused target is reached on a fresh attempt and the
    # aggregate becomes ACKNOWLEDGED with BOTH targets enumerated as reached.
    git(dirty.path, "checkout", "--", "canon.md")
    third = propagator(synthetic).run_all(targets)
    assert third is not None and third.state is State.ACKNOWLEDGED, third
    assert set(third.reached) == {"replica", "dirty-authoring"}
    assert third.not_reached == ()
    assert git(dirty.path, "rev-parse", "HEAD") == new
    assert head_moves(clean.path) == 1


def test_a_cursor_the_journal_never_acknowledged_is_a_corrupted_sink_state_and_raises(
    synthetic: Synthetic,
) -> None:
    # Found in review: the "corrupted sink state" branch was unwitnessed, so a silent
    # `return None` in its place kept the whole file green while dropping a declared
    # target from the aggregate -- the same outcome class the previous fix closed.
    dest = replica(synthetic, "replica")
    new = synthetic.push_change("canon v2\n")
    coord = coordinate(dest.destination_id)
    # Forge the one state the journal can never produce: a cursor AT the source
    # revision with no acknowledged attempt behind it.
    Journal(synthetic.state_dir).advance_cursor(coord.key(), new, pending_id="forged")
    before = snapshot(dest.path)

    with pytest.raises(JournalInconsistent, match="no acknowledged attempt"):
        propagator(synthetic).run_all([(coord, dest)])
    with pytest.raises(JournalInconsistent, match="no acknowledged attempt"):
        propagator(synthetic).run(coord, dest)
    assert snapshot(dest.path) == before, "a refusal to guess must not touch the destination"

    # Control: a cursor the journal DID acknowledge is the ordinary current state --
    # run() reports nothing new and run_all reports the target as already reached.
    healthy = replica(synthetic, "healthy")
    hcoord = coordinate(healthy.destination_id)
    first = propagator(synthetic).run(hcoord, healthy)
    assert first is not None and first.state is State.ACKNOWLEDGED
    assert propagator(synthetic).run(hcoord, healthy) is None
    again = propagator(synthetic).run_all([(hcoord, healthy)])
    assert again is not None and again.state is State.ACKNOWLEDGED
    assert again.reached == ("healthy",) and again.receipts[0].replayed is True


def test_run_all_with_no_declared_targets_is_none(synthetic: Synthetic) -> None:
    synthetic.push_change("canon v2\n")
    assert propagator(synthetic).run_all([]) is None


def test_an_explicit_empty_git_env_is_inherit_and_not_the_isolated_default(
    synthetic: Synthetic,
) -> None:
    # None asks for the default (isolated from the host); {} asks to inherit the host
    # environment. {} is falsy, so an `or` would silently turn inherit into isolated and a
    # caller that needs the host's credential helper would lose it without any error.
    assert "GIT_CONFIG_GLOBAL" in propagator(synthetic).git_env
    assert propagator(synthetic, git_env=None).git_env == propagator(synthetic).git_env
    assert propagator(synthetic, git_env={}).git_env == {}
    explicit = {"GIT_TERMINAL_PROMPT": "0"}
    assert propagator(synthetic, git_env=explicit).git_env == explicit
    # and the inherit form still drives the machine: a change is applied under {} as well
    dest = replica(synthetic, "replica")
    new = synthetic.push_change("canon v2\n")
    receipt = propagator(synthetic, git_env={}).run(coordinate(dest.destination_id), dest)
    assert receipt is not None and receipt.state is State.ACKNOWLEDGED
    assert git(dest.path, "rev-parse", "HEAD") == new


def test_all_destinations_verifying_is_acknowledged_not_partial(synthetic: Synthetic) -> None:
    a = replica(synthetic, "replica-a")
    b = replica(synthetic, "replica-b")
    synthetic.push_change("canon v2\n")
    outcome = propagator(synthetic).run_all(
        [(coordinate(a.destination_id), a), (coordinate(b.destination_id), b)]
    )
    assert outcome.state is State.ACKNOWLEDGED
    assert set(outcome.reached) == {"replica-a", "replica-b"}
    assert outcome.not_reached == ()


# ------------------------------------------------- operation identity


def test_operation_id_binds_coordinate_source_base_and_digest(synthetic: Synthetic) -> None:
    a = replica(synthetic, "replica-a")
    b = replica(synthetic, "replica-b")
    new = synthetic.push_change("canon v2\n")
    ra = propagator(synthetic).run(coordinate(a.destination_id), a)
    rb = propagator(synthetic).run(coordinate(b.destination_id), b)
    assert ra is not None and rb is not None
    # same source revision, same base, same digest: the coordinate differs, so the id differs
    assert ra.operation_id != rb.operation_id
    expected = hashlib.sha256(
        json.dumps(
            {
                "coordinate": coordinate(a.destination_id).key(),
                "source_rev": new,
                "expected_base": synthetic.base,
                "digest": ra.digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert ra.operation_id == expected


# ------------------------------------------------ coordinate key is injective


def _coord(**overrides: object) -> Coordinate:
    base: dict[str, object] = {
        "source": "source",
        "destination": "target",
        "layer": "layer",
        "direction": Direction.DOWN,
        "operation": Operation.APPLY,
        "artifact_class": "class",
    }
    return Coordinate(**{**base, **overrides})  # type: ignore[arg-type]


def _joined_with_pipes(c: Coordinate) -> str:
    """The scheme the first cut used, kept here ONLY as the control."""
    return "|".join(
        (c.source, c.destination, c.layer, str(c.direction), str(c.operation), c.artifact_class)
    )


# Every pair below is two DISTINCT coordinates whose fields, joined with "|",
# produced ONE key on the first cut. The first pair is the reviewer's exact
# discriminator; the second shifts the byte across the source/destination/layer
# boundaries in one move; the third shows the enum fields are not a fence either,
# because an opaque field may itself contain the enum words.
_COLLIDING_ON_THE_OLD_SCHEME = [
    (_coord(source="source|dest"), _coord(destination="dest|target")),
    (_coord(layer="layer|down"), _coord(source="source|target", destination="layer", layer="down")),
    (
        _coord(artifact_class="class|down|apply|z"),
        _coord(layer="layer|down|apply|class", artifact_class="z"),
    ),
]

# Bytes JSON itself uses for structure, in the opaque fields: these never collided
# under the old scheme, so they carry no control; they exist to show the new
# encoding escapes them and round-trips regardless.
_JSON_STRUCTURE_BYTES = [
    _coord(source='a","destination":"b'),
    _coord(source="a\\"),
    _coord(source="a\\\\"),
    _coord(destination='{"source":"x"}'),
    _coord(layer="|", artifact_class="|"),
]


@pytest.mark.parametrize("left,right", _COLLIDING_ON_THE_OLD_SCHEME)
def test_distinct_coordinates_never_share_a_key(left: Coordinate, right: Coordinate) -> None:
    assert left != right, "the pair must be two different coordinates or it proves nothing"
    # Control first: the pair MUST collide under the old scheme, or the witness is
    # not discriminating and a green here would say nothing about the fix.
    assert _joined_with_pipes(left) == _joined_with_pipes(right)
    assert left.key() != right.key()


@pytest.mark.parametrize(
    "coord",
    [c for pair in _COLLIDING_ON_THE_OLD_SCHEME for c in pair] + _JSON_STRUCTURE_BYTES,
)
def test_coordinate_key_round_trips_so_injectivity_is_structural(coord: Coordinate) -> None:
    # Injectivity is asserted as a ROUND TRIP, not as "this pair differs": if every
    # key decodes back to the coordinate that produced it, no two coordinates can
    # share one, whatever bytes the opaque fields carry.
    assert Coordinate.from_key(coord.key()) == coord
    assert json.loads(coord.key()) == coord.as_dict()


def test_distinct_coordinates_never_share_cursor_or_journal_rows(tmp_path: Path) -> None:
    # The consequence the review named: the key names the cursor file and the journal
    # rows, so a shared key is shared replay state. Advance one; the other is untouched.
    left, right = _COLLIDING_ON_THE_OLD_SCHEME[0]
    journal = Journal(tmp_path / "state")
    assert journal.cursor_path(left.key()) != journal.cursor_path(right.key())
    journal.advance_cursor(left.key(), "a" * 40, pending_id="p")
    assert journal.cursor(left.key()) == "a" * 40
    assert journal.cursor(right.key()) is None
    journal.note(
        pending_id="p", attempt=1, coordinate_key=left.key(), source_rev="a" * 40, note="only-left"
    )
    assert journal.notes(left.key(), "a" * 40, "only-left") == 1
    assert journal.notes(right.key(), "a" * 40, "only-left") == 0


# ---------------------------------------------------- outbox survival witness
#
# Born red as a strict xfail, and flipped when the consumer cursor moved from
# read_events() to ack_events(): an event whose effect did not happen is offered again.


class EventLost(AssertionError):
    """Raised ONLY by the survival check below.

    While the witness was a strict xfail it was the marker's ``raises=`` constraint, so
    that a premise failure (no event emitted, nothing offered on the first read) could not
    satisfy the marker. It is kept so a real loss still reads as EventLost and a broken
    premise as a plain AssertionError.
    """


def _offered_types(workspace: Path) -> list[str]:
    return [e["type"] for e in read_events(workspace, "propagation-consumer")]


def test_outbox_offers_an_emitted_event_to_a_fresh_consumer(tmp_path: Path) -> None:
    # The PREMISE of the born-red witness, as its own ordinary test: an emitted
    # event is offered on the first read. If this goes red the outbox regressed
    # upstream of the cursor question and the witness below is not about that.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    emit(
        EventType.SYNC_COMPLETED,
        workspace,
        actor="sink",
        owner_unit="unit-0",
        payload={"detail": "one"},
    )
    assert _offered_types(workspace) == ["sync.completed"]


def test_outbox_event_survives_a_consumer_that_fails_after_reading(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    emit(
        EventType.SYNC_COMPLETED,
        workspace,
        actor="sink",
        owner_unit="unit-0",
        payload={"detail": "one"},
    )

    # PREMISE (plain asserts: a failure here is a FAILURE, not the expected xfail)
    with pytest.raises(RuntimeError):
        offered = _offered_types(workspace)
        assert offered == ["sync.completed"], f"premise: nothing offered, got {offered!r}"
        raise RuntimeError("effect failed after the read")

    # SURVIVAL CHECK, the only statement allowed to satisfy the marker: an event
    # whose effect did not happen is offered again
    again = _offered_types(workspace)
    if again != ["sync.completed"]:
        raise EventLost(f"offered once, then lost: second read returned {again!r}")


# -- grip#893: a torn journal line must not be fatal, and must not eat its neighbour
#
# The writer appends and fsyncs, so a kill between the write and the fsync leaves
# a partial trailing line. Three witnesses, each for a distinct failure:
#
#   W1  the tear must not destroy the NEXT row (measured on the filed issue: with
#       no newline repair the following append glues onto the remnant, so a row
#       that was written correctly and fsynced becomes unreadable)
#   W2  reading a torn journal must not raise; the intact prefix still answers
#   W3  an idle tick must not re-parse a journal that has not changed
#
# W1 is the one the issue does not contain and is the reason the position rule in
# it does not hold: after a restart append the malformed line is no longer last.


def _tear(journal: Journal, partial: str = '{"kind": "not-final') -> None:
    """Leave a partial trailing line, exactly as a kill between write and fsync does."""
    with journal.path.open("a") as handle:
        handle.write(partial)
        handle.flush()
        os.fsync(handle.fileno())


def test_a_torn_line_does_not_swallow_the_row_written_after_it(tmp_path: Path) -> None:
    """W1. The append after a tear must not glue onto the remnant.

    Without a newline repair the file becomes ``…{"n": 3, "no{"n": 4}`` — row 4 was
    written correctly and fsynced, and is unreadable because row 3 died mid-line.
    A tear may cost the interrupted write; it must never cost the next one.
    """
    journal = Journal(tmp_path / "state")
    journal._write({"n": 1})
    journal._write({"n": 2})
    _tear(journal)

    journal._write({"n": 4})

    rows = journal._rows()
    assert {r.get("n") for r in rows} == {1, 2, 4}, (
        "the row written after the tear must survive it; "
        f"file was {journal.path.read_text()!r}"
    )


def test_a_torn_trailing_line_is_skipped_and_reported_not_raised(tmp_path: Path) -> None:
    """W2. Reading a torn journal answers from the intact prefix and says what it dropped.

    The daemon lets anything outside its named failure list propagate, so a bare
    ``JSONDecodeError`` out of the parse exits the loop on the first tick and on
    every restart after it. The intact prefix is still a truthful answer.
    """
    journal = Journal(tmp_path / "state")
    journal._write({"n": 1})
    journal._write({"n": 2})
    _tear(journal)

    rows = journal._rows()

    assert [r.get("n") for r in rows] == [1, 2]
    assert len(journal.malformed_lines) == 1, "the drop must be counted, not silent"
    assert journal.malformed_lines[0].index == 2


def test_an_unchanged_journal_is_not_reparsed_on_the_next_tick(tmp_path: Path) -> None:
    """W3. Prototype 1 consults the machine on every tick, including idle ones.

    That is correct — a corrupted sink behind an unchanged source must not read as
    healthy — but it moved the whole-file parse from once-per-change to once-per-
    tick. An unchanged file must cost nothing to re-read; a changed one must not
    be served from a stale cache.
    """
    journal = Journal(tmp_path / "state")
    journal._write({"n": 1})
    journal._write({"n": 2})

    parsed: list[str] = []
    real_loads = json.loads

    def counting_loads(payload, *args, **kwargs):  # type: ignore[no-untyped-def]
        parsed.append(payload)
        return real_loads(payload, *args, **kwargs)

    import gr2.prototypes.propagation_state_machine as module

    original = module.json.loads
    module.json.loads = counting_loads
    try:
        first = journal._rows()
        after_first = len(parsed)
        second = journal._rows()
        after_second = len(parsed)

        journal._write({"n": 3})
        third = journal._rows()
        after_third = len(parsed)
    finally:
        module.json.loads = original

    assert after_first == 2, "the first read parses the file"
    assert after_second == after_first, "an unchanged journal must not be re-parsed"
    assert first == second
    assert after_third > after_second, "a changed journal must not be served stale"
    assert [r.get("n") for r in third] == [1, 2, 3]
