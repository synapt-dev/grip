# Assess Sync Adversarial Specs

Artifact 2 for the Sprint 20 sync lane.

This document lists the failure-first specs the Python `gr2 sync` implementation
must satisfy before `sync run` is allowed to mutate workspace state.

## 1. Missing Spec

Preconditions:
- workspace has no `.grip/workspace_spec.toml`

Trigger:
- `gr2 sync status <workspace>`

Expected:
- command fails immediately
- error points to `gr2 workspace init`
- no cache, repo, lane, or event state is written

## 2. Partial Clone Failure

Preconditions:
- spec declares 3 repos
- repo A and B are reachable
- repo C remote is invalid or unavailable

Trigger:
- `gr2 sync run`

Expected:
- planner marks A/B runnable and C failing before execution starts
- execution stops on C if C is in the same phase batch
- result reports:
  - A/B success or skipped state explicitly
  - C as failure with repo-scoped error payload
- no successful repo update is silently rolled back
- event outbox records partial progress and terminal failure

Invariant:
- sync never reports all-green on partial workspace failure

## 3. Dirty Shared Repo

Preconditions:
- shared repo checkout exists
- uncommitted changes in repo root

Trigger:
- `gr2 sync status`

Expected:
- issue `dirty_shared_repo`
- issue blocks sync
- planner does not schedule branch movement or fetch-dependent mutation through
  the dirty checkout

Invariant:
- dirty state wins over convenience

## 4. Dirty Lane Checkout During Sync

Preconditions:
- lane checkout exists
- lane repo has uncommitted changes

Trigger:
- `gr2 sync status`

Expected:
- issue `dirty_lane_repo`
- issue blocks sync
- planner may still inspect other repos, but lane mutation is blocked

Invariant:
- lane-local work is never overwritten by workspace sync

## 5. Conflicting Branch States Across Repos

Preconditions:
- lane spans repos `app`, `api`, `premium`
- expected branch is `feat/auth`
- `app` is on `feat/auth`
- `api` is behind remote
- `premium` is on a different local branch

Trigger:
- `gr2 sync status`

Expected:
- planner reports repo-scoped branch inspection operations
- branch divergence appears as explicit sync issue, not implicit correction
- no automatic branch checkout/rebase in status mode

Invariant:
- branch alignment must be explicit before mutation

## 6. Shared Cache Path Conflict

Preconditions:
- `.grip/cache/repos/<repo>.git` exists
- path is not a bare git directory

Trigger:
- `gr2 sync status`

Expected:
- issue `cache_path_conflict`
- sync blocks
- planner does not attempt to reuse or overwrite the invalid cache path

## 7. Invalid Repo Hook Config

Preconditions:
- shared repo has `.gr2/hooks.toml`
- file does not parse or violates schema

Trigger:
- `gr2 sync status`

Expected:
- spec validation fails before sync planning proceeds
- sync status returns blocked with the hook validation error included

Invariant:
- repo hook errors fail fast at plan time

## 8. Sync During Active Edit Lease

Preconditions:
- lane has an active `edit` lease
- lane repo is otherwise clean

Trigger:
- `gr2 sync run --lane <lane>`

Expected:
- sync refuses lane mutation for the leased lane
- non-lane workspace inspection may still succeed
- result clearly distinguishes lease-blocked lanes from unrelated workspace
  status

Invariant:
- sync does not tunnel through active edit occupancy

## 9. Concurrent Sync From Two Worktrees

Preconditions:
- same workspace available from two operator shells
- both invoke sync against overlapping repos

Trigger:
- `gr2 sync run` concurrently

Expected:
- shared mutable resources use explicit lock discipline
- losing side returns machine-readable contention error
- no cache corruption, no partially-written apply metadata

Invariant:
- concurrency failure is reported, not hidden as random repo damage

## 10. Platform Backend Failure

Preconditions:
- `PlatformAdapter` backend is GitHub via `gh`
- `gh` auth is invalid or the command times out

Trigger:
- sync planner tries to refresh PR/check state

Expected:
- repo/local sync inspection still reports local status
- platform-dependent operations are marked degraded or failed
- failure is explicit in the result payload

Invariant:
- adapter failure must not masquerade as clean workspace state
