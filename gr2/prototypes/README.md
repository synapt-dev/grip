# gr2 Repo Maintenance + Collaboration Prototypes

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
- shared scratchpads for lightweight collaboration

Example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py create-lane \
  /path/to/workspace atlas feat-auth --repos app,api --branch feat/auth

python3 gr2/prototypes/lane_workspace_prototype.py plan-exec \
  /path/to/workspace atlas feat-auth 'cargo test'
```

Review lane example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py create-review-lane \
  /path/to/workspace atlas grip 548
```

Shared scratchpad example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py create-shared-scratchpad \
  /path/to/workspace blog-s17 \
  --kind doc \
  --purpose "Sprint 17 blog draft" \
  --participant atlas \
  --participant layne \
  --ref grip#552

python3 gr2/prototypes/lane_workspace_prototype.py list-shared-scratchpads \
  /path/to/workspace
```

This prototype still does not execute commands. It proves that lane and
scratchpad metadata can become the durable source of truth for:

- which repos belong to a lane
- which branch each repo should use
- which context roots apply
- where multi-repo commands should run
- where lightweight collaboration should happen without violating private
  workspaces

## UX Focus

This prototype is intentionally trying to answer user-facing questions:

- how do I create a review lane quickly?
- how do I know what I should do next in this lane?
- when should I use a shared scratchpad instead of a PR or a private lane?

That is why it includes:

- `list-lanes`
- `next-step`
- `create-review-lane`
- `create-shared-scratchpad`

## Stress Testing

This prototype is not considered verified on the happy path alone.

The break-case matrix lives at:

- `docs/ASSESS-gr2-shared-scratchpads-stress.md`

The MVP should not be finalized until the prototype has been evaluated against:

- concurrent shared editing
- stale / abandoned scratchpads
- wrong-surface selection
- scope creep into shared implementation
- cleanup and lifecycle handling
- promotion from scratchpad to real repo artifact / PR

## Real Git Verification

Prototype confidence should not stop at metadata or tempdir-only happy paths.

The next verification phase should use real GitHub repos in `synapt-dev`:

- `synapt-dev/gr2-playground-app`
- `synapt-dev/gr2-playground-api`
- `synapt-dev/gr2-playground-web`

These repos exist specifically to pressure the UX against actual git behavior:

- cloning and default branches
- multi-repo branch switching
- review-lane isolation
- dirty-work detection and recovery
- shared scratchpad usage alongside private lanes

That real-git verification phase is now tracked in:

- `grip#523`
- `grip#555`

The design standard should be:

- prototype behavior must survive both synthetic stress cases and real-repo
  workflow checks before the MVP is treated as solid

## Cross-Mode Lane Stress

The lane model also needs adversarial verification across the four primary
operating modes:

- solo human
- single agent
- multi-agent
- mixed human + agent

Run:

```bash
python3 gr2/prototypes/cross_mode_lane_stress.py
```

This harness does not just show happy-path lane creation. It reports where the
current model:

- holds
- partially holds
- still fails

across interruption recovery, same-repo parallelism, mixed-mode conflicts, and
lane-recovery ambiguity.

The current prototype adds two explicit recovery/safety concepts to support
that stress loop:

- lane session state
  - `enter-lane`
  - `current-lane`
- lane leases
  - `acquire-lane-lease`
  - `release-lane-lease`
  - `show-lane-leases`

Those are still prototype surfaces, but they let us test whether the lane
model can survive interruption and mixed human/agent use rather than only
describe those needs abstractly.

Lease behavior is now explicit in the prototype:

- leases have `ttl_seconds`
- stale leases are detectable
- `acquire-lane-lease --force` can break stale conflicting leases
- the conflict matrix is deliberate:
  - `edit` conflicts with `edit`, `exec`, and `review`
  - `exec` conflicts with `edit` and `review`, but not `exec`
- `review` is exclusive

## Synapt Integration Prototype

The lane prototype now includes a minimal Synapt-native event layer:

- `enter-lane --notify-channel --recall`
- `exit-lane --notify-channel --recall`
- append-only event log at `.grip/events/lane_events.jsonl`
- recall-compatible log at `.grip/events/recall_lane_history.jsonl`
- `lane-history` to reconstruct a unit's lane timeline

Unit `agent_id` can now flow from `WorkspaceSpec` into:

- lane metadata
- current-lane state
- emitted lane events

This is still prototype scope, but it tests the right product direction:
lane transitions and lease changes should be observable workspace events rather
than invisible local state.

Agent handoff example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py share-lane \
  /path/to/workspace atlas feat-router apollo

python3 gr2/prototypes/lane_workspace_prototype.py plan-handoff \
  /path/to/workspace atlas feat-router apollo --mode shared --json

python3 gr2/prototypes/lane_workspace_prototype.py create-continuation-lane \
  /path/to/workspace atlas feat-router apollo feat-router-relay

python3 gr2/prototypes/lane_workspace_prototype.py plan-handoff \
  /path/to/workspace atlas feat-router apollo --mode continuation \
  --target-lane-name feat-router-relay --json
```

Current prototype conclusion:

- cross-unit shared-lane relay violates the unit-scoping invariant
- continuation lanes preserve unit-scoped cwd, lease scope, and current-lane state
- handoff should preserve lineage to the source lane without forcing the target
  unit to execute inside the source unit's lane root

Identity rebind example:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py rebind-unit \
  /path/to/workspace synapt-core release-control --actor premium:control-plane --json
```

Current prototype conclusion:

- active lanes under the old unit stay in place and become frozen
- active leases are force-released and logged during the rebind
- old-unit exec planning is blocked after rebind
- recovery should happen through continuation lanes under the new unit
- the minimal safe contract from premium is:
  - same `agent_id` continuity
  - explicit old -> new unit mapping
  - pending-reassignment hint is recommended to reduce operator surprise

Identity -> unit binding example:

```bash
python3 gr2/prototypes/identity_unit_binding.py demo
python3 gr2/prototypes/identity_unit_binding.py resolve-binding ws_synapt_core opus --json
python3 gr2/prototypes/identity_unit_binding.py compile-workspace ws_synapt_core --json
```

This prototype keeps the premium boundary hard:

- Premium owns persistent agent identity, org membership, and workspace assignment.
- gr2 only consumes the compiled workspace-scoped unit view.
- the same persistent agent can map to different `owner_unit` names in different
  workspaces without gr2 learning org logic.
- reassignment is a premium recompilation event, not a gr2-side identity
  decision.

Org/policy compiler example:

```bash
python3 gr2/prototypes/org_policy_compiler.py demo
python3 gr2/prototypes/org_policy_compiler.py compile --scenario baseline --json
python3 gr2/prototypes/org_policy_compiler.py compile --scenario repo-update --json
python3 gr2/prototypes/org_policy_compiler.py compile --scenario downgrade --json
```

This prototype keeps the compiler seam explicit:

- Premium reads org config, roles, entitlements, and reviewer policy.
- Premium outputs workspace-scoped constraints that gr2 can enforce locally.
- gr2 sees unit repo access, lane limits, and workspace constraints, but not the
  raw org policy logic that produced them.

Recall lane history example:

```bash
python3 gr2/prototypes/recall_lane_history.py demo-data /tmp/gr2-recall-demo
python3 gr2/prototypes/recall_lane_history.py query /tmp/gr2-recall-demo --lane auth-refactor --json
python3 gr2/prototypes/recall_lane_history.py query /tmp/gr2-recall-demo --actor agent:atlas --json
python3 gr2/prototypes/recall_lane_history.py query /tmp/gr2-recall-demo --repo grip --json
```

This prototype indexes lane events into a neutral recall-friendly timeline:

- by lane
- by actor
- by repo
- by time range

Recall can answer lane-history questions from structured workspace events
without importing premium identity or org semantics.

## Real-Git Same-Repo Multi-Agent Materialization

To verify that unit-local-first is real and not only metadata, run:

```bash
python3 gr2/prototypes/real_git_lane_materialization.py
```

This harness:

- creates a local bare remote
- writes a workspace spec that points at it
- creates two lanes for two different units that both touch the same repo
- verifies `plan-exec` produces distinct cwd paths
- clones into those lane cwd paths
- makes independent commits in each checkout
- verifies the checkouts do not interfere

## Concurrent Lease Stress

To verify that lease writes survive contention, run:

```bash
python3 gr2/prototypes/concurrent_lease_stress.py
```

This harness runs two phases in one command:

- before locking: `GR2_DISABLE_LEASE_LOCKING=1`
- after locking: default locking enabled

It reports:

- JSON corruption count
- rounds where both conflicting edit acquisitions succeeded
- rounds where the final lease count was wrong

Bootstrap command:

```bash
python3 gr2/prototypes/real_git_playground.py /tmp/gr2-real-git-demo
```

If the local environment cannot reach GitHub over SSH, use:

```bash
python3 gr2/prototypes/real_git_playground.py /tmp/gr2-real-git-demo \
  --transport https
```

That harness will:

- initialize a fresh gr2 workspace
- register the three private playground repos
- write a real `WorkspaceSpec`
- run `plan` and `apply`
- create real local git branches in the cloned repos
- create multiple lanes and one shared scratchpad
- print repo, lane, exec, and scratchpad status surfaces

## New UX-Focused Prototype Surfaces

The prototype now includes explicit user-guidance commands for the cases that
usually break first in real workflows:

```bash
python3 gr2/prototypes/lane_workspace_prototype.py recommend-surface \
  --kind doc --collaborative --shared-draft

python3 gr2/prototypes/lane_workspace_prototype.py audit-shared-scratchpads \
  /path/to/workspace --stale-days 3

python3 gr2/prototypes/lane_workspace_prototype.py plan-promote-scratchpad \
  /path/to/workspace blog-s17 \
  --target-repo app \
  --target-path docs/blog/sprint-17.md \
  --owner-unit atlas
```

These are intentionally user-first:

- `recommend-surface`
  - answers "should this be a feature lane, review lane, or shared scratchpad?"
- `audit-shared-scratchpads`
  - exposes stale, orphaned, or weakly tracked scratchpads
- `plan-promote-scratchpad`
  - makes the graduation path from shared draft to repo artifact explicit

This keeps the prototype from overfitting to happy-path metadata creation while
ignoring the actual decisions users struggle with.

## Transport/Auth Preflight

Real multi-repo bootstrap fails early if transport or auth is wrong, so the
prototype now includes a dedicated preflight surface:

```bash
python3 gr2/prototypes/repo_transport_probe.py \
  /path/to/workspace/.grip/workspace_spec.toml
```

This reports, per repo:

- transport type
- whether the remote looks reachable
- whether auth is failing
- the next recommended action

The real-git playground harness now runs this probe before `gr2 apply` so
transport/auth problems are surfaced as an explicit status surface instead of a
late clone failure buried inside apply output.

## Layout Model Probe

The real-git playground also needs to answer a harder product question:

- does the observed layout actually match the mental model we are designing?

The prototype now includes:

```bash
python3 gr2/prototypes/layout_model_probe.py /path/to/workspace --owner-unit atlas
```

This compares the observed workspace against two candidate models:

- shared-repo-first
- unit-local-first

It is intentionally blunt. If the workspace behaves like one model while the
docs imply another, the prototype should say so directly.

## Cache Materialization Probe

The next question is whether shared cache as apply substrate is actually worth
it in practice.

The prototype now includes:

```bash
python3 gr2/prototypes/cache_materialization_probe.py --transport ssh
```

This measures, per playground repo:

- direct remote clone time
- one-time mirror seed time
- cache-backed working clone time using `git clone --reference-if-able`
- whether the resulting working clone actually uses alternates

This keeps the cache discussion grounded in evidence:

- lanes remain the UX
- cache remains the optimization
- the prototype should tell us whether the optimization is material enough to
  justify building it into `apply`
