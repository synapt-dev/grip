# Grip Object Model Phase 0 Test Plan

Issue: `grip#606`

Scope: Phase 0 validates the git-native snapshot model only. No Automerge, no daemon, no runtime overlays. The goal is to prove that `gr` can snapshot a multi-repo workspace into a dedicated `.grip/` repo and make that state reviewable and recoverable.

## Commands under test

- `gr grip`
- `gr show`
- `gr diff`
- `gr checkout`

## Correctness criteria

### `gr grip`

`gr grip` is correct when it:
- creates a dedicated `.grip/` git repo on first run
- writes a new grip commit whose tree records every repo in the workspace
- records the repo HEAD commit for each tracked repo
- is additive: a crash before ref update must not leave a partially visible snapshot
- refuses or clearly reports invalid preconditions instead of silently guessing

### `gr show`

`gr show <ref>` is correct when it:
- renders a single snapshot in a human-reviewable form
- includes repo names and the recorded commit/branch state
- works against the current snapshot and explicit historical refs
- fails clearly if the grip repo or requested ref is missing

### `gr diff`

`gr diff <a> <b>` is correct when it:
- reports which repos changed between two grip snapshots
- does not drown the user in raw object noise
- keeps output reviewable as ordinary text
- handles the “no changes” case clearly

### `gr checkout`

`gr checkout <grip-ref>` is correct when it:
- restores repo working state to the recorded snapshot
- leaves repos already at the correct commit untouched
- handles detached-HEAD restoration explicitly
- fails clearly if the target snapshot is missing or invalid

## Adversarial cases Apollo must satisfy

### Snapshot creation

- clean 5-repo workspace snapshots successfully
- dirty workspace snapshots either:
  - fail explicitly, or
  - succeed under an explicit mode only
- missing `.grip/` repo bootstraps cleanly
- missing tracked repo path fails clearly
- repo on detached HEAD is either recorded explicitly or rejected with a clear error

### Snapshot diff/show

- diff between two snapshots with one changed repo is concise and reviewable
- diff between identical snapshots reports “no changes”
- show on a corrupted grip repo fails clearly

### Restore

- checkout restores prior repo commit after a later workspace change
- checkout from corrupted grip ref fails without mutating workspace repos
- interrupted or partial snapshot writes do not create a visible target ref

## Phase 0 gate

Phase 0 is complete only if:
- snapshot creation works in a disposable 5-repo gripspace
- show/diff output is human-reviewable
- restore works from a prior snapshot
- corrupted or partial snapshot state fails safely
- all behavior is proven without Automerge or Rust runtime additions
