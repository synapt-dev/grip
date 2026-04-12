# gr2 Repo Maintenance Prototype

This prototype explores a split between:

- `gr2 apply`
  - structural workspace convergence
  - create/mount missing repo paths
  - write unit metadata
- repo maintenance
  - fetch, fast-forward, branch correction, dirty-worktree handling

## Why it exists

`WorkspaceSpec v1` is intentionally narrow. It tells us which repos should
exist and where they should live, but it does not yet define full collaboration
policy for:

- when to pull
- when to fast-forward
- when to refuse because a repo is dirty
- when autostash is allowed
- when a branch mismatch is safe to correct automatically

The prototype keeps those decisions explicit instead of burying them inside
`gr2 apply`.

## Command

```bash
python3 gr2/prototypes/repo_maintenance_prototype.py /path/to/workspace
```

Optional policy file:

```bash
python3 gr2/prototypes/repo_maintenance_prototype.py \
  /path/to/workspace \
  --policy /path/to/repo_policy.toml
```

## Example policy

```toml
[defaults]
tracked_branch = "main"
shared_sync = "ff-only"
unit_sync = "explicit"
dirty = "block"

[repos.grip]
dirty = "autostash"
```

## Current behavior

The planner classifies each shared repo and each unit-mounted repo into actions
such as:

- `clone_missing`
- `fast_forward`
- `checkout_branch`
- `block_dirty`
- `autostash_then_sync`
- `manual_sync`
- `no_change`

That gives us a concrete design target for future commands like:

- `gr2 repo status`
- `gr2 repo sync`
- `gr2 repo pull`

without turning plain `gr2 apply` into an unsafe catch-all mutation command.

## Lane Workspace Prototype

`lane_workspace_prototype.py` explores the next layer above repo maintenance:

- explicit lane metadata on disk
- lane-local repo membership and branch map
- shared + private context roots
- lane-aware execution planning

Example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py create-lane \
  /path/to/workspace atlas feat-auth --repos app,api --branch feat/auth

python3 gr2/prototypes/lane_workspace_prototype.py plan-exec \
  /path/to/workspace atlas feat-auth 'cargo test'
```

This prototype does not execute commands. It proves that lane metadata can
become the durable source of truth for:

- which repos belong to a lane
- which branch each repo should use
- which context roots apply
- where multi-repo commands should run
