# Sync Failure Contract

Artifact 3 for the Sprint 20 sync lane.

This contract defines what Python `gr2 sync` is allowed to do on failure, what
it must report, and what it must never attempt to hide.

## 1. Core Rule

`sync status` is read-only.

`sync run` may mutate workspace state, but it must never pretend a partial
failure is a rollback-complete success.

## 2. Mutation Model

`sync` operates in ordered phases:

1. inspect
2. plan
3. execute
4. emit result + outbox events

Within a phase, successful mutations are durable unless the operation itself has
an explicit local rollback mechanism.

Examples:
- a completed `git fetch` is durable
- a completed cache refresh is durable
- a completed clone is durable
- a completed branch checkout is durable

These are not automatically rolled back just because a later repo fails.

## 3. Default Failure Behavior

On the first blocking failure in `sync run`:

- stop scheduling new mutating operations in the current batch
- preserve already-completed successful operations
- report all completed work explicitly
- report the blocking failure explicitly
- write an event/outbox record describing the partial state

The contract is:
- stop
- preserve
- report

Not:
- guess
- continue blindly
- fabricate rollback

## 4. Dirty State

Dirty handling is explicit through `--dirty=stash|block|discard`.

Default:
- `--dirty=stash`

Behavior:
- `stash`: preserve local work by stashing it before sync mutation proceeds
- `block`: return a blocking dirty-state issue and do not mutate through that
  checkout
- `discard`: explicitly discard local changes before sync mutation proceeds

Rules:
- no implicit commit
- no dirty-state behavior outside the declared `--dirty` mode
- `discard` is always explicit and never the default

## 5. Partial State Contract

If `sync run` partially succeeds:

- result status is `partial_failure`
- result contains:
  - completed operations
  - blocked operations
  - failed operations
  - unaffected operations, if known
- event outbox must include:
  - `sync_started`
  - one event per completed mutation
  - `sync_failed`

Consumers must be able to reconstruct:
- what changed
- what did not change
- what needs human or agent follow-up

## 6. Rollback Rules

Default rule:
- no automatic workspace-wide rollback

Reason:
- cross-repo rollback is not reliably safe
- later repos may fail after earlier repos perform valid, independent updates
- forcing rollback would risk clobbering legitimate state

Allowed rollback only when all of the following are true:
- rollback scope is local to one operation
- rollback is deterministic
- rollback result can be verified immediately
- rollback failure is itself reportable

Examples of acceptable local rollback candidates later:
- removing a just-created empty metadata file
- deleting a just-created lane marker that has no downstream references yet

Examples not allowed by default:
- resetting git refs across multiple repos
- auto-restoring stashes across partially-mutated lane trees
- deleting refreshed caches because a later repo failed

## 7. Error Reporting Contract

Every blocking failure must carry:
- `code`
- `scope`
- `subject`
- human-readable `message`
- machine-readable `details` when available

Every sync result must distinguish:
- `blocked` from policy/safety preconditions
- `failed` from runtime execution errors
- `partial_failure` from all-or-nothing failure

## 8. Lease and Occupancy Contract

If sync encounters an active conflicting lease:
- it is a blocker, not a warning
- sync does not override or steal the lease
- result points to the owning actor and lease mode when available
- `sync.conflict` is emitted with the blocking lease metadata
- terminal state still arrives through `sync.completed` with `status = "blocked"`

If a stale lease policy is added later, it must be explicit and separately
authorized. It is not part of the default sync contract.

## 9. Platform Adapter Failure Contract

If the `PlatformAdapter` backend fails:
- local repo and lane inspection still completes when possible
- platform-derived fields are marked degraded/failed
- sync status must not silently omit missing platform data

GitHub via `gh` is treated as an external dependency:
- failures are surfaced
- not normalized away

## 10. Operator Expectations

When `sync` fails, the operator should be able to answer:

1. what changed?
2. what did not change?
3. what blocked the next step?
4. what is safe to retry?

If the result payload cannot answer those four questions, the sync surface is
not ready for production mutation.
