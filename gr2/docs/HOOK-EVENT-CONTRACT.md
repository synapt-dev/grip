# gr2 Hook/Event Contract

This document defines the event contract for gr2: what events the system emits,
their schema, delivery model, and how consumers (spawn, recall, channel bridge)
integrate.

This is a **design document** for Sprint 20. It does not describe current
behavior; it defines the target contract.

## 1. Design Goals

- Every gr2 operation that changes workspace state emits a typed event.
- Events are durable, append-only, and replayable.
- Consumers read events at their own pace via cursors. gr2 does not block on
  delivery.
- The event schema is the stable API between OSS gr2 and premium spawn.
- Hook execution is one event source among several, not the only one.

## 2. Event Sources

gr2 emits events from five operational domains:

| Domain | Examples | Current State |
|--------|----------|---------------|
| **Lane lifecycle** | lane.created, lane.entered, lane.exited, lane.archived | Partial (SYNAPT-INTEGRATION.md defines format, not wired) |
| **Lease lifecycle** | lease.acquired, lease.released, lease.expired, lease.force_broken | Prototype only |
| **Hook execution** | hook.started, hook.completed, hook.failed | hooks.py runs commands but emits nothing |
| **PR lifecycle** | pr.created, pr.status_changed, pr.merged, pr.checks_passed | Missing (Sprint 20 deliverable) |
| **Sync operations** | sync.started, sync.repo_updated, sync.completed, sync.conflict | Missing (Atlas's sync algorithm design) |

Each domain owns a namespace prefix. Events are globally ordered by timestamp
and monotonic sequence number within the outbox.

## 3. Event Schema

### 3.1 Common Envelope

Every event is a single flat JSON object. Envelope fields and domain-specific
fields sit at the same level. There is no nested `payload` wrapper.

```json
{
  "version": 1,
  "event_id": "a1b2c3d4e5f6",
  "seq": 42,
  "timestamp": "2026-04-15T16:30:00+00:00",
  "type": "lane.entered",
  "workspace": "synapt-dev",
  "actor": "agent:apollo",
  "agent_id": "agent_apollo_xyz789",
  "owner_unit": "apollo",
  "lane_name": "feat/hook-events",
  "lane_type": "feature",
  "repos": ["grip", "synapt"]
}
```

This flat shape matches Atlas's sync outbox implementation (`syncops.py`), where
`_append_outbox_event` spreads caller-provided fields into the envelope via
`{**envelope, **payload}`. Consumers read domain fields directly from the
top-level object without unwrapping a nested payload.

**Envelope fields** (added automatically by the emit function):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | int | yes | Schema version. Always `1` for this contract. |
| `event_id` | string | yes | Unique event identifier. 16-char hex from `os.urandom(8).hex()`. |
| `seq` | int | yes | Monotonically increasing sequence number within this outbox file. Starts at 1. |
| `timestamp` | string | yes | ISO 8601 with timezone. |
| `type` | string | yes | Dotted event type from the taxonomy (section 3.2). |

**Context fields** (provided by the caller, required unless noted):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace` | string | yes | Workspace name from WorkspaceSpec. |
| `actor` | string | yes | Who triggered the event. Format: `agent:<name>`, `human:<name>`, or `system`. |
| `agent_id` | string | no | Persistent agent identity from premium. Opaque in OSS. |
| `owner_unit` | string | yes | Unit that owns the context where this event occurred. |

**Domain fields** vary by event type. See section 3.2 for the fields each event
type carries. Domain fields are top-level keys alongside envelope and context
fields.

Rules:
- `event_id` must be unique within a workspace.
- `seq` must be strictly monotonically increasing within a single outbox file.
- `actor` uses the prefix convention to distinguish agents from humans from
  automated operations.
- `agent_id` is optional because human-triggered and system-triggered events
  do not have one.
- Domain field names must not collide with envelope or context field names.
  The reserved names are: `version`, `event_id`, `seq`, `timestamp`, `type`,
  `workspace`, `actor`, `agent_id`, `owner_unit`.

### 3.2 Event Type Taxonomy

#### Lane Lifecycle

| Type | Trigger | Payload |
|------|---------|---------|
| `lane.created` | `gr2 lane create` | `{lane_name, lane_type, repos: [str], branch_map: {repo: branch}}` |
| `lane.entered` | `gr2 lane enter` | `{lane_name, lane_type, repos: [str]}` |
| `lane.exited` | `gr2 lane exit` | `{lane_name, stashed_repos: [str]}` |
| `lane.switched` | Enter a different lane (exit + enter) | `{from_lane, to_lane, stashed_repos: [str]}` |
| `lane.archived` | Lane cleanup after merge | `{lane_name, reason}` |

#### Lease Lifecycle

| Type | Trigger | Payload |
|------|---------|---------|
| `lease.acquired` | `gr2 lane lease acquire` | `{lane_name, mode, ttl_seconds, lease_id}` |
| `lease.released` | `gr2 lane lease release` | `{lane_name, lease_id}` |
| `lease.expired` | TTL watchdog or next acquire check | `{lane_name, lease_id, expired_at}` |
| `lease.force_broken` | `--force` acquire or admin break | `{lane_name, lease_id, broken_by, reason}` |

#### Hook Execution

| Type | Trigger | Payload |
|------|---------|---------|
| `hook.started` | Lifecycle hook begins execution | `{stage, hook_name, repo, command, cwd}` |
| `hook.completed` | Hook exits successfully | `{stage, hook_name, repo, duration_ms, exit_code: 0}` |
| `hook.failed` | Hook exits with non-zero code | `{stage, hook_name, repo, duration_ms, exit_code, on_failure, stderr_tail}` |
| `hook.skipped` | Hook `when` condition not met | `{stage, hook_name, repo, reason}` |

Rules for hook events:
- `stderr_tail` is the last 500 bytes of stderr, truncated. Full output is not
  stored in the event.
- `on_failure` records the policy that was applied (block, warn, skip).
- `hook.failed` with `on_failure: "block"` means the parent operation also
  failed. Consumers should expect a corresponding operation failure event.

#### PR Lifecycle

| Type | Trigger | Payload |
|------|---------|---------|
| `pr.created` | `gr2 pr create` | `{pr_group_id, repos: [{repo, pr_number, url, title, base, head}]}` |
| `pr.status_changed` | Poll or webhook | `{pr_group_id, repo, pr_number, old_status, new_status}` |
| `pr.checks_passed` | All CI checks green | `{pr_group_id, repo, pr_number}` |
| `pr.checks_failed` | CI check failure | `{pr_group_id, repo, pr_number, failed_checks: [str]}` |
| `pr.review_submitted` | Review posted | `{pr_group_id, repo, pr_number, reviewer, verdict}` |
| `pr.merged` | `gr2 pr merge` | `{pr_group_id, repos: [{repo, pr_number, merge_sha}]}` |
| `pr.merge_failed` | Merge blocked or conflict | `{pr_group_id, repo, pr_number, reason}` |

`pr_group_id` is the cross-repo correlation key. When `gr2 pr create` creates
PRs in multiple repos, they share the same `pr_group_id`. This is how consumers
reconstruct the cross-repo PR as a unit.

**Boundary**: `pr_group_id` is assigned by gr2's orchestration layer (`pr.py`),
not by PlatformAdapter. PlatformAdapter is group-unaware: it creates, queries,
and merges individual per-repo PRs. The `pr.py` module correlates them into a
group and assigns the `pg_` prefixed ID. This keeps platform adapters simple and
reusable across contexts that may not need grouping.

#### Sync Operations

| Type | Trigger | Payload |
|------|---------|---------|
| `sync.started` | `gr2 sync` begins | `{repos: [str], strategy}` |
| `sync.repo_updated` | Single repo pull/rebase completes | `{repo, old_sha, new_sha, strategy, commits_pulled: int}` |
| `sync.repo_skipped` | Repo skipped (dirty, no remote, etc.) | `{repo, reason}` |
| `sync.conflict` | Merge/rebase conflict during sync | `{repo, conflicting_files: [str]}` |
| `sync.completed` | `gr2 sync` finishes | `{status, repos_updated: int, repos_skipped: int, repos_failed: int, duration_ms}` |

`sync.completed` is the **single terminal event** for sync operations. There is no
separate `sync.failed` type. The `status` field distinguishes outcomes:

| `status` value | Meaning |
|----------------|---------|
| `success` | All repos updated without error. |
| `partial_failure` | Some repos updated, some failed. `repos_failed > 0`. |
| `blocked` | Sync could not proceed (e.g., unresolved failure marker). |
| `failed` | All repos failed or sync aborted early. |

This matches Atlas's `syncops.py` pattern, which uses `sync.completed` with a
status field rather than emitting a separate `sync.failed` event type.

#### Recovery

| Type | Trigger | Payload |
|------|---------|---------|
| `failure.resolved` | `gr2 lane resolve <operation_id>` | `{operation_id, resolved_by, resolution, lane_name}` |
| `lease.reclaimed` | Stale lease garbage-collected during acquire | `{lane_name, lease_id, previous_holder, expired_at, reclaimed_by}` |

`failure.resolved` is emitted when an agent explicitly clears a failure marker
(section 14.1). `lease.reclaimed` is emitted when a stale lease is
garbage-collected during a new acquire (section 14.2, step 6-7). This is
distinct from `lease.expired` (which fires at the point of staleness detection)
and `lease.force_broken` (which fires when a live lease is broken with
`--force`).

#### Workspace Operations

| Type | Trigger | Payload |
|------|---------|---------|
| `workspace.materialized` | `gr2 workspace materialize` or `gr2 apply` | `{repos: [{repo, first_materialize: bool}]}` |
| `workspace.file_projected` | File link/copy applied | `{repo, kind, src, dest}` |

### 3.3 Payload Conventions

- All paths in payloads are relative to `workspace_root`, never absolute.
- Repo names match `WorkspaceSpec` `[[repos]]` names, not filesystem paths.
- SHA values are full 40-char hex.
- Duration values are in milliseconds as integers.
- String arrays are used for repo lists, file lists, etc. Never comma-separated strings.

## 4. Event Outbox

### 4.1 Storage

Events are written to a single append-only JSONL file:

```
.grip/events/outbox.jsonl
```

One JSON object per line. No trailing commas. No array wrapper.

The outbox file is the single source of truth for all gr2 events in a workspace.

### 4.2 Write Path

Events are written synchronously at the point of state change:

1. Operation performs its work (e.g., creates a lane, runs a hook).
2. Operation calls `emit_event(type, payload)`.
3. `emit_event` assigns `event_id`, `seq`, `timestamp`.
4. Event is serialized and appended to `outbox.jsonl`.
5. File is flushed (fsync not required; OS page cache is sufficient for
   local-only delivery).

`seq` is derived from the current line count of the outbox file plus one. This
is safe because gr2 operations are single-process. If concurrent writers become
necessary (multiple agents in the same workspace), `seq` assignment must move to
a lock or use a separate sequence file.

### 4.3 Rotation

When `outbox.jsonl` exceeds 10 MB:

1. Rename to `outbox.{timestamp}.jsonl`.
2. Create new empty `outbox.jsonl` with `seq` continuing from the last value.
3. Old files are retained for 7 days, then eligible for cleanup by `gr2 gc`.

Consumers must handle rotation by checking for new files when their cursor
points past the end of the current file.

### 4.4 No Deletion

Events are never deleted from the outbox. They are append-only. Rotation moves
old events to archived files but does not remove them. `gr2 gc` is the only
operation that removes archived event files, and only after the retention period.

## 5. Consumer Model

### 5.1 Cursor-Based Reading

Each consumer maintains a cursor file in `.grip/events/cursors/`:

```
.grip/events/cursors/{consumer_name}.json
```

Cursor format:

```json
{
  "consumer": "channel_bridge",
  "last_seq": 41,
  "last_event_id": "a1b2c3d4e5f6",
  "last_read": "2026-04-15T16:31:00+00:00"
}
```

Reading flow:

1. Consumer opens cursor file (or starts at seq 0 if no cursor exists).
2. Consumer reads `outbox.jsonl` from line `last_seq + 1` forward.
3. Consumer processes each event.
4. Consumer updates cursor atomically (write temp file, rename).

### 5.2 Known Consumers

| Consumer | Location | What It Does |
|----------|----------|--------------|
| **channel_bridge** | OSS | Derives `#dev`-style notifications from events. Posts to channel transport. |
| **recall_indexer** | OSS | Indexes events into recall for searchable lane/activity history. |
| **spawn_watcher** | Premium | Watches for events that trigger agent orchestration (lane assignments, PR readiness, hook failures). |

### 5.3 Consumer Contract

Consumers must:
- Be idempotent. Re-processing the same event (e.g., after a crash before
  cursor update) must produce the same result.
- Use `event_id` for deduplication if their target store does not naturally
  deduplicate.
- Not modify or delete events in the outbox.
- Handle unknown event types gracefully (skip, log, do not crash).
- Handle schema version bumps by checking `version` and ignoring events with
  a version they do not understand.

### 5.4 Spawn Integration (Premium)

Spawn is the premium consumer that orchestrates multi-agent workflows. It
consumes the same outbox as OSS consumers but interprets events through the
lens of org policy and agent identity.

Events that spawn cares about:

| Event | Spawn Reaction |
|-------|----------------|
| `lane.created` | May assign agent to lane based on policy. |
| `pr.created` | May assign reviewers based on compiled review requirements. |
| `pr.checks_passed` | May trigger merge if auto-merge policy is active. |
| `pr.checks_failed` | May notify owning agent or escalate. |
| `hook.failed` with `on_failure: "block"` | May retry, reassign, or alert. |
| `lease.expired` | May reclaim the lane or notify the agent. |
| `sync.conflict` | May pause agent work on conflicting repos. |

Spawn does not write to the outbox. Spawn's actions (assigning agents,
triggering merges) flow back through the gr2 CLI, which then emits its own
events. This prevents circular event chains.

## 6. Hook Execution Contract

This section formalizes the relationship between hook execution (hooks.py) and
event emission.

### 6.1 Current State

`hooks.py` currently:
- Parses `.gr2/hooks.toml`
- Resolves template variables
- Runs commands via `subprocess.run`
- Raises `SystemExit` on `on_failure: "block"` failures
- Prints JSON on `on_failure: "warn"` failures
- Does nothing on `on_failure: "skip"` failures

It does **not** emit structured events.

### 6.2 Target State

Every hook execution produces events:

```
hook.started -> (command runs) -> hook.completed | hook.failed
```

If the hook's `when` condition is not met:

```
hook.skipped
```

The lifecycle stage runner (`run_lifecycle_stage`) becomes the event emitter.
After running all hooks for a stage, it emits the parent lifecycle event
(e.g., `lane.entered`) with a summary of hook results in the payload.

### 6.3 Hook Output Capture

Hook commands produce stdout and stderr. The event contract does not store full
output in events (it would bloat the outbox). Instead:

- `hook.completed` includes `duration_ms` and `exit_code: 0`.
- `hook.failed` includes `duration_ms`, `exit_code`, `on_failure` policy, and
  `stderr_tail` (last 500 bytes).
- Full stdout/stderr is written to:
  ```
  .grip/events/hook_output/{event_id}.stdout
  .grip/events/hook_output/{event_id}.stderr
  ```
- Hook output files follow the same retention policy as rotated outbox files.

### 6.4 Hook Failure Propagation

When a hook fails with `on_failure: "block"`:

1. `hook.failed` event is emitted with `on_failure: "block"`.
2. The parent operation (e.g., `workspace.materialized`) is **not** emitted
   because the operation did not complete.
3. Instead, the calling code should emit a domain-specific failure event
   (e.g., `sync.conflict` or handle it in its own error path).

When a hook fails with `on_failure: "warn"`:

1. `hook.failed` event is emitted with `on_failure: "warn"`.
2. The parent operation continues and eventually emits its success event.
3. Consumers can correlate the `hook.failed` event with the parent by timestamp
   and `owner_unit` context.

When a hook fails with `on_failure: "skip"`:

1. `hook.failed` event is emitted with `on_failure: "skip"`.
2. No consumer-visible notification. The event exists for audit trail only.

## 7. Event Emission API

### 7.1 Python Interface

```python
from gr2.events import emit, EventType

# Simple emission
emit(
    event_type=EventType.LANE_ENTERED,
    workspace_root=workspace_root,
    actor="agent:apollo",
    owner_unit="apollo",
    payload={
        "lane_name": "feat/hook-events",
        "lane_type": "feature",
        "repos": ["grip", "synapt"],
    },
)

# With optional agent_id
emit(
    event_type=EventType.HOOK_FAILED,
    workspace_root=workspace_root,
    actor="agent:apollo",
    agent_id="agent_apollo_xyz789",
    owner_unit="apollo",
    payload={
        "stage": "on_materialize",
        "hook_name": "editable-install",
        "repo": "synapt",
        "duration_ms": 3400,
        "exit_code": 1,
        "on_failure": "block",
        "stderr_tail": "ERROR: pip install failed ...",
    },
)
```

### 7.2 EventType Enum

```python
class EventType(str, Enum):
    # Lane lifecycle
    LANE_CREATED = "lane.created"
    LANE_ENTERED = "lane.entered"
    LANE_EXITED = "lane.exited"
    LANE_SWITCHED = "lane.switched"
    LANE_ARCHIVED = "lane.archived"

    # Lease lifecycle
    LEASE_ACQUIRED = "lease.acquired"
    LEASE_RELEASED = "lease.released"
    LEASE_EXPIRED = "lease.expired"
    LEASE_FORCE_BROKEN = "lease.force_broken"

    # Hook execution
    HOOK_STARTED = "hook.started"
    HOOK_COMPLETED = "hook.completed"
    HOOK_FAILED = "hook.failed"
    HOOK_SKIPPED = "hook.skipped"

    # PR lifecycle
    PR_CREATED = "pr.created"
    PR_STATUS_CHANGED = "pr.status_changed"
    PR_CHECKS_PASSED = "pr.checks_passed"
    PR_CHECKS_FAILED = "pr.checks_failed"
    PR_REVIEW_SUBMITTED = "pr.review_submitted"
    PR_MERGED = "pr.merged"
    PR_MERGE_FAILED = "pr.merge_failed"

    # Sync operations
    SYNC_STARTED = "sync.started"
    SYNC_REPO_UPDATED = "sync.repo_updated"
    SYNC_REPO_SKIPPED = "sync.repo_skipped"
    SYNC_CONFLICT = "sync.conflict"
    SYNC_COMPLETED = "sync.completed"

    # Recovery
    FAILURE_RESOLVED = "failure.resolved"
    LEASE_RECLAIMED = "lease.reclaimed"

    # Workspace operations
    WORKSPACE_MATERIALIZED = "workspace.materialized"
    WORKSPACE_FILE_PROJECTED = "workspace.file_projected"
```

### 7.3 Implementation Location

The event emission module lives at:

```
gr2/python_cli/events.py
```

This module owns:
- `emit()` function
- `EventType` enum
- Outbox file management (append, rotation, seq tracking)
- Cursor read helpers for consumers

It does **not** own consumer logic. Each consumer is a separate module.

## 8. Channel Bridge Event Mapping

The channel bridge translates gr2 events into channel messages. Not every event
produces a channel message.

| Event | Channel Message | Channel |
|-------|----------------|---------|
| `lane.created` | `"{actor} created lane {lane_name} [{lane_type}] repos={repos}"` | #dev |
| `lane.entered` | `"{actor} entered {owner_unit}/{lane_name}"` | #dev |
| `lane.exited` | `"{actor} exited {owner_unit}/{lane_name}"` | #dev |
| `pr.created` | `"{actor} opened PR group {pr_group_id}: {repos}"` | #dev |
| `pr.merged` | `"{actor} merged PR group {pr_group_id}"` | #dev |
| `pr.checks_failed` | `"CI failed on {repo}#{pr_number}: {failed_checks}"` | #dev |
| `hook.failed` (block) | `"Hook {hook_name} failed in {repo} (blocking): {stderr_tail}"` | #dev |
| `sync.conflict` | `"Sync conflict in {repo}: {conflicting_files}"` | #dev |
| `lease.force_broken` | `"Lease on {lane_name} force-broken by {broken_by}: {reason}"` | #dev |
| `failure.resolved` | `"{resolved_by} resolved failure {operation_id} on {lane_name}"` | #dev |
| `lease.reclaimed` | `"Stale lease on {lane_name} reclaimed (was held by {previous_holder})"` | #dev |

Events not listed (hook.started, hook.completed, hook.skipped, lease.acquired,
lease.released, sync.repo_updated, workspace.file_projected, etc.) are **not**
posted to channels by default. They exist in the outbox for recall indexing and
spawn, but would be noise in `#dev`.

The channel bridge can be configured to include or exclude specific event types
via a filter file at `.grip/events/channel_filter.toml`:

```toml
[channel_bridge]
include = ["lane.*", "pr.*", "hook.failed", "sync.conflict", "lease.force_broken", "failure.resolved", "lease.reclaimed"]
exclude = ["hook.started", "hook.completed", "hook.skipped"]
```

Default: the mapping table above. Filter file is optional.

## 9. Recall Indexing

Recall indexes all events (not just the channel-visible subset) for searchable
history. The recall indexer is a cursor-based consumer that:

1. Reads new events from the outbox.
2. Indexes each event by: lane, actor, repo, event type, and time range.
3. Stores indexed events in recall's existing storage layer.

Query examples that this enables:

- `recall_files(path="grip/src/main.rs")` can include "last sync updated this
  file" if sync events include file-level detail.
- `recall_search("hook failure editable-install")` returns the hook.failed event
  and its context.
- `recall_timeline(actor="agent:apollo", start="2026-04-15")` shows Apollo's
  full activity timeline.

The recall indexer does **not** need premium logic. It consumes the same neutral
event stream as the channel bridge.

## 10. Failure Modes and Recovery

### 10.1 Outbox Write Failure

If `emit_event` fails to append (disk full, permission error):

- The event is lost. The operation that triggered it still completed.
- The outbox may be in an inconsistent state (partial line written).
- Recovery: consumers skip malformed lines. `gr2 gc` can truncate trailing
  partial lines.

Mitigation: `emit_event` should catch write errors and log them to stderr
without crashing the parent operation. Events are important but not
operation-critical.

### 10.2 Consumer Crash Mid-Processing

If a consumer crashes after reading an event but before updating its cursor:

- On restart, it re-reads from `last_seq + 1` and reprocesses events.
- This is safe because consumers must be idempotent (section 5.3).

### 10.3 Outbox Rotation During Consumer Read

If the outbox rotates while a consumer is reading:

- The consumer's cursor points to a seq that no longer exists in the current
  `outbox.jsonl`.
- Consumer must scan archived `outbox.{timestamp}.jsonl` files in order to find
  the file containing its cursor position.
- Once caught up through archived files, it continues reading the current
  `outbox.jsonl`.

### 10.4 Concurrent Writers

The current design assumes single-process writes (one gr2 CLI invocation at a
time per workspace). If concurrent writes become necessary:

- Option A: File-level advisory lock during append.
- Option B: Separate outbox files per writer, with a merge step.
- Option C: Move to SQLite WAL-mode database.

This is explicitly out of scope for the initial implementation. The single-writer
assumption is safe because gr2 operations are CLI-driven and workspace-local.

## 11. Versioning and Evolution

### 11.1 Schema Version

The `version` field in the event envelope is `1` for this initial contract.

Version bumps happen when:
- A required field is added to the common envelope.
- A payload field's type or meaning changes in a breaking way.

Version bumps do **not** happen when:
- A new event type is added (consumers skip unknown types).
- An optional field is added to a payload.
- A new consumer is added.

### 11.2 Backward Compatibility

New event types are additive. Consumers that do not understand a new type skip
it. This means adding `pr.review_submitted` in a future release does not require
updating all consumers.

Payload changes within an existing event type should be additive (new optional
fields). If a breaking change is needed, bump the version and document the
migration.

## 12. Relation to Existing Documents

This document supersedes the event-related sections of:

- **SYNAPT-INTEGRATION.md** section 4 (Lane Event -> Recall Pipeline): This
  contract formalizes and extends that design. The event format here is the
  canonical schema; SYNAPT-INTEGRATION.md's examples are now illustrative only.
- **SYNAPT-INTEGRATION.md** section 5 (Channel Bridge): The channel bridge model
  here is consistent but more precise about filtering and cursor management.

This document builds on:

- **HOOK-CONFIG-MODEL.md**: The hook execution contract (section 6) extends the
  lifecycle model defined there. hooks.toml schema is unchanged; the new
  contribution is event emission during hook execution.

This document is a dependency for:

- **PR-LIFECYCLE.md** (Sprint 20, Apollo): PR lifecycle design references the
  pr.* event types defined here.
- **PLATFORM-ADAPTER-AND-SYNC.md** (Sprint 20, Atlas): Sync algorithm references
  the sync.* event types defined here.
- **QA Arena** (Sprint 20, Sentinel): Adversarial test scenarios should exercise
  event emission failure modes (section 10).

## 13. Open Questions

1. **Hook output retention**: Should hook output files (`.grip/events/hook_output/`)
   follow the same 7-day retention as rotated outbox files, or longer?
2. **Event batching**: Should operations that touch multiple repos emit one event
   per repo or one aggregate event? Current design uses both patterns depending
   on the domain (sync uses per-repo events; PR uses aggregate events with
   per-repo detail in payload arrays).
3. **Webhook bridge**: Should gr2 support an HTTP webhook consumer in addition to
   file-based cursor consumers? This would be relevant for remote spawn
   deployments.
4. **SQLite alternative**: For workspaces with heavy event traffic (many agents,
   frequent operations), should the outbox be SQLite WAL instead of JSONL?
   JSONL is simpler and auditable; SQLite handles concurrent writes better.
5. **Event signing**: Should events carry a signature or checksum for tamper
   detection? Relevant if the outbox is consumed by premium policy enforcement.

## 14. Failure Recovery Contract

This section formalizes how gr2 handles operation failures at the state level.
Section 10 covers event infrastructure failures (outbox writes, consumer
crashes). This section covers operation-level failures: what happens to
workspace state when hooks fail, leases expire, or lane switches encounter
dirty repos.

The core principle: **gr2 operations are forward-only. There is no rollback.**
Failures leave partial state with explicit markers that require resolution.

### 14.1 Failure Markers

When an operation fails mid-execution, gr2 writes a failure marker:

```
.grip/state/failures/{operation_id}.json
```

Marker format:

```json
{
  "operation_id": "op_9f2a3b4c",
  "operation": "sync",
  "stage": "on_enter",
  "hook_name": "editable-install",
  "repo": "synapt",
  "owner_unit": "apollo",
  "lane_name": "feat/hook-events",
  "failed_at": "2026-04-15T17:00:00+00:00",
  "event_id": "abc123def456",
  "partial_state": {
    "repos_completed": ["grip"],
    "repos_pending": ["synapt-private"],
    "repo_failed": "synapt"
  },
  "resolved": false
}
```

Marker behavior:

- **Blocking**: The next operation on the same scope (lane, repos) checks for
  unresolved failure markers. If one exists, the operation refuses to proceed
  and reports the marker.
- **Resolution**: `gr2 lane resolve <operation_id>` clears the marker. The
  agent must decide whether to retry, skip, or escalate. Resolution is always
  explicit.
- **Event**: Resolving a marker emits a new event type:
  `failure.resolved` with payload `{operation_id, resolved_by, resolution}`.

Why no automatic retry: retrying a failed hook might produce the same failure.
The agent (or spawn) has context about whether retry is appropriate. gr2 does
not guess.

Why no rollback: reverting git operations (undo fetch+merge, undo checkout) is
dangerous, sometimes impossible (remote state changed), and introduces a second
failure mode (what if the revert fails?). Forward-only resolution is simpler and
more honest about what happened.

### 14.2 Lease Reclaim Lifecycle

Leases use TTL-first expiry with optional heartbeat renewal.

**TTL expiry** is the primary reclaim mechanism:

- Every lease carries `ttl_seconds` (default 900s) and `expires_at`.
- Expiry is checked lazily: the next `acquire`, `show`, or `status` call
  evaluates `is_stale_lease()` (already in prototype at
  `lane_workspace_prototype.py:592`).
- No daemon or background process required.

**Heartbeat renewal** is optional:

- `gr2 lane lease renew <workspace_root> <owner_unit> <lane_name>` resets
  `expires_at` to `now + ttl_seconds`.
- Agents running long operations (multi-repo test suites, large builds) call
  renew periodically to prevent premature expiry.
- If the agent crashes, renewal stops, and TTL expiry reclaims the lease
  naturally.

**Reclaim flow**:

1. Agent A holds lease with `expires_at = T`.
2. Agent A crashes (no explicit release).
3. Time passes beyond T.
4. Agent B calls `gr2 lane lease acquire`.
5. `acquire` finds A's lease, evaluates `is_stale_lease()` -> true.
6. Emits `lease.expired` event (payload: `{lane_name, lease_id, expired_at}`).
7. Garbage-collects A's stale lease from the lane doc.
8. Grants B's new lease. Emits `lease.acquired` event.

**Force break**:

- `gr2 lane lease acquire --force` breaks a live (non-expired) lease.
- Emits `lease.force_broken` event with `{broken_by, reason}`.
- Notification routing to the original holder is a **channel_bridge consumer
  responsibility**, not a core gr2 concern. The `lease.force_broken` event
  carries `broken_by` and the original holder's identity in context fields.
  The channel bridge (or spawn_watcher) decides how and where to deliver the
  notification based on its own routing rules.

### 14.3 Dirty State on Lane Switch

Lane transitions handle uncommitted changes via an explicit `--dirty` mode.

**Modes** (flag on `lane enter` and `lane exit`):

| Mode | Behavior | Default? |
|------|----------|----------|
| `stash` | Auto-stash dirty repos. Stash message: `"gr2 auto-stash: exiting {unit}/{lane}"`. | Yes |
| `block` | Refuse to switch if any repo is dirty. List dirty repos in error. | No |
| `discard` | Discard uncommitted changes. Requires `--yes` flag. | No |

**Event payloads for dirty state**:

- `lane.exited` with `stashed_repos: ["synapt"]` when stash mode is used.
- `lane.exited` with `discarded_repos: ["synapt"]` when discard mode is used.
- No `lane.exited` event when block mode prevents the exit.

**Re-entry with stashed state**:

When `lane enter` is called and the lane has stashed state from a previous exit:

- Default: warn that stashed state exists, do not auto-pop. The agent decides
  whether to `git stash pop` manually.
- `--dirty=restore` on `lane enter`: auto-pop the stash. If the pop produces
  a merge conflict, leave the conflict markers and emit a `hook.failed`-style
  warning event.

**Consistency rule**: The `--dirty` flag and its values (`stash`, `block`,
`discard`, `restore`) must be consistent across `lane enter`, `lane exit`, and
`sync`. This is a shared contract with Atlas's sync algorithm design.
