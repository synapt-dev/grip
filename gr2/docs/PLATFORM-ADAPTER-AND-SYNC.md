# Platform Adapter And Sync

Sprint 20 design lane for:

- `PlatformAdapter` protocol
- GitHub-only shipping backend for `gr2 2.0`
- sync algorithm for cross-repo orchestration

Required companion artifacts for this design:

- adversarial failing specs:
  [ASSESS-SYNC-ADVERSARIAL-SPECS.md](./ASSESS-SYNC-ADVERSARIAL-SPECS.md)
- failure/rollback contract:
  [SYNC-FAILURE-CONTRACT.md](./SYNC-FAILURE-CONTRACT.md)

## 1. Scope

`gr2` owns cross-repo orchestration in OSS:

- workspace spec
- materialization
- sync
- lanes
- aggregated status
- PR orchestration

Single-repo git remains raw git.

Platform integration is intentionally narrow:

- ship GitHub only first
- hide platform details behind a protocol
- let future GitLab / Azure / Bitbucket adapters arrive later without changing `gr2` UX

## 2. Adapter Contract

`gr2/python_cli/platform.py` defines the protocol:

- `create_pr`
- `merge_pr`
- `pr_status`
- `list_prs`
- `pr_checks`

The CLI consumes the protocol only. It does not talk to GitHub directly.

### Shipping backend

The first backend is `GitHubAdapter`, implemented on top of `gh` CLI.

Reasoning:

- simplest path to production
- no custom API client to maintain
- reuses existing authenticated operator environment
- keeps platform logic thin while we prove the orchestration UX

### Future plugin path

The adapter boundary is intentionally protocol-shaped, not GitHub-shaped.

That makes third-party adapters possible later:

- config-based adapter selection
- module import / entry-point registration
- same `gr2` PR commands, different backend implementation

## 3. Required Spawn-Readiness Seams

For premium spawn to move on top of `gr2`, these are required:

- hook invocation API with stable structured results
- workspace / lane event outbox
- leases and lane metadata
- `exec status` and `exec run`
- machine-readable failure surfaces

These are not optional polish. They are spawn prerequisites.

## 4. Sync Goals

`sync` is the missing orchestration surface between:

- spec/plan/apply
- lane state
- repo caches
- review/PR flow

`sync` must be:

- safe with dirty state
- lane-aware
- explicit about what it mutates
- resumable after partial failure

## 5. Sync Phases

### Phase A: Inspect

Read:

- workspace spec
- shared repo cache state
- shared repo checkout state
- lane metadata
- lease state
- hook configs

Emit a workspace-level snapshot:

- missing repos
- stale caches
- dirty repos
- lane checkouts missing
- lane branches behind remote
- hook config errors

### Phase B: Plan

Build a sync plan with explicit operations:

- refresh repo cache
- fast-forward shared repo
- materialize missing repo
- refresh lane branch
- block on dirty state
- block on conflicting lease
- surface manual action required

No mutation yet.

### Phase C: Execute

Apply only safe operations by default:

- fetch/update cache
- clone missing repo
- materialize missing lane checkout
- fast-forward clean branches

Unsafe operations must block unless explicitly requested:

- dirty shared repo
- dirty lane checkout
- branch divergence requiring merge/rebase
- hook failure with `on_failure = block`

### Phase D: Emit

Write:

- structured sync result
- event outbox entries
- updated aggregated status snapshot

This is the seam premium and QA will consume.

## 6. Sync Safety Rules

1. Dirty state wins over convenience.
   If a repo is dirty, `sync` blocks instead of mutating through it.

2. Lanes are first-class.
   `sync` must treat shared repos and lane checkouts differently.

3. Shared repo cache is substrate, not UX.
   Mutations there should be invisible unless they affect user work.

4. Partial failure must be reportable.
   Example: 3 of 5 repos updated, 1 blocked dirty, 1 platform failure.

5. Event emission is part of correctness.
   `sync` must emit enough machine-readable state for premium spawn and QA.

## 7. Proposed Command Shapes

Initial surfaces:

- `gr2 sync status`
- `gr2 sync run`

Possible later flags:

- `--lane <name>`
- `--owner-unit <unit>`
- `--refresh-prs`
- `--allow-dirty-stash`
- `--json`

`sync status` should be the dry-run/default read path.

`sync run` should consume the same planner output and execute allowed operations.

## 8. Failure Scenarios The QA Arena Must Cover

- dirty shared repo during sync
- dirty lane checkout during sync
- lane branch behind remote
- lane branch diverged from remote
- `gh` timeout during PR create/status
- partial repo refresh failure
- hook failure during sync-triggered materialization
- concurrent sync from two worktrees
- sync during active edit lease

These are required Sprint 20 QA inputs, not later hardening.

## 9. Implementation Ordering

I agree with Layne's platform-first ordering, with one constraint:

1. `PlatformAdapter` protocol + `GitHubAdapter`
2. sync algorithm design with event outbox requirements folded in
3. aggregated status
4. PR create/status/merge on the adapter
5. lane switch/list polish

Rationale:

- PR lifecycle should not be implemented before the adapter boundary exists
- sync and aggregated status share most of the same inspection model
- event outbox requirements need to be considered while designing sync, not bolted on later

## 10. Non-Goals

Not part of Sprint 20 `gr2` OSS:

- single-repo git porcelain
- spawn/agent orchestration
- release flow
- multi-platform support beyond GitHub

Those would either duplicate raw git or blur the OSS/premium boundary.
