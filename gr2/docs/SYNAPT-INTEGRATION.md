# Synapt Integration

This document defines the integration surface between:

- Premium
  - durable identity
  - org policy
  - entitlements
  - control-plane compilation
- OSS gr2
  - local workspace materialization
  - unit and lane enforcement
  - lane events
  - execution surfaces
- OSS recall
  - indexing and querying neutral lane event history

The key rule is simple:

- Premium compiles.
- OSS consumes.

`gr2` should feel native inside a Synapt workspace, but it must not absorb
Premium-only identity or org logic.

## 1. Architecture Overview

The data flow is:

```text
Premium org config + identity + policy + entitlements
    -> compiler
    -> WorkspaceSpec + lane/unit constraints
    -> gr2 workspace materialization + lane enforcement
    -> lane event log
    -> recall indexing + channel bridge
```

Operationally:

1. Premium resolves durable agent identity and workspace assignment.
2. Premium compiles org rules into workspace-scoped constraints.
3. `gr2` materializes the workspace and enforces unit/lane behavior locally.
4. `gr2` emits neutral lane events.
5. Recall indexes those events into searchable lane history.
6. The channel bridge derives `#dev`-style notifications from the same event log.

The important layering rule:

- Premium is the source of truth for identity, org policy, and entitlement
  evaluation.
- `gr2` is the source of truth for local workspace state and lane execution.
- Recall is the source of truth for searchable event history derived from local
  workspace events.

## 2. Identity Binding Contract

### What Premium Provides

Premium resolves:

- `handle`
- `persistent_id`
- workspace membership
- workspace assignment
- `owner_unit`
- role
- repo scope
- lane limits

This binding is workspace-scoped. The same persistent agent may map to
different `owner_unit` names in different workspaces.

Example:

- `opus` with `persistent_id = agent_opus_abc123`
  - `owner_unit = synapt-core` in workspace `ws_synapt_core`
  - `owner_unit = editorial-opus` in workspace `ws_blog`

Reassignment is also Premium-owned:

- Premium may change `opus` from `synapt-core` to `release-control`
- `gr2` does not infer or own that change
- `gr2` simply consumes the recompiled workspace view

### What gr2 Consumes

`gr2` consumes a workspace-scoped unit record:

```json
{
  "name": "release-control",
  "path": "agents/release-control",
  "agent_id": "agent_opus_abc123",
  "repos": ["grip", "premium", "recall"],
  "constraints": {
    "lane_limit": 2
  }
}
```

Rules:

- `agent_id` is opaque attribution data in OSS
- `owner_unit` is the local workspace identity
- `gr2` must not perform org resolution from `agent_id`
- `gr2` must not infer cross-workspace identity mapping

Living prototype:

- [identity_unit_binding.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/identity_unit_binding.py)

## 3. Org/Policy Compilation

### Premium Input Schema

Premium evaluates:

- `team_id`
- workspace id and name
- repo set
- agent roster
- roles
- entitlements
- policy rules

The prototype models:

- global max concurrent edit leases
- role-based repo access
- lane naming convention
- required reviewers per repo

Example premium-side input shape:

```json
{
  "team_id": "team_synapt_core",
  "workspace_id": "ws_synapt_core",
  "repos": ["grip", "premium", "recall", "config", "tests"],
  "agents": [
    {
      "handle": "opus",
      "persistent_id": "agent_opus_abc123",
      "role": "builder",
      "entitlements": ["premium", "channels", "recall", "multi_lane"],
      "owner_unit": "release-control"
    }
  ],
  "policy": {
    "max_concurrent_edit_leases_global": 2,
    "lane_naming_convention": "<kind>-<scope>",
    "required_reviewers": {
      "premium": 2,
      "grip": 1
    }
  }
}
```

### OSS Output Schema

Premium compiles that into a workspace-scoped `WorkspaceSpec` fragment plus
unit constraints:

```json
{
  "workspace_name": "synapt-core",
  "workspace_id": "ws_synapt_core",
  "repos": [
    {"name": "grip", "path": "repos/grip"}
  ],
  "units": [
    {
      "name": "release-control",
      "path": "agents/release-control",
      "agent_id": "agent_opus_abc123",
      "repos": ["grip", "premium", "recall"],
      "constraints": {
        "lane_limit": 3,
        "allowed_lane_kinds": ["feature", "review", "scratch"],
        "channels_enabled": true,
        "recall_enabled": true
      }
    }
  ],
  "workspace_constraints": {
    "max_concurrent_edit_leases_global": 2,
    "lane_naming_convention": "<kind>-<scope>",
    "required_reviewers": {
      "premium": 2,
      "grip": 1
    }
  }
}
```

### Scenarios The Compiler Must Handle

The prototype covers:

1. Baseline org
   - 3 agents
   - 5 repos
   - max 2 concurrent edit leases globally
2. Role-based repo access
   - builders get all repos
   - QA gets test-focused access only
3. Repo update mid-sprint
   - new repo added
   - recompile updates affected units
4. Entitlement downgrade
   - premium removed
   - unit degrades gracefully to OSS defaults

Important downgrade rule:

- Premium decides the degradation policy
- `gr2` only enforces the compiled downgraded result

Living prototype:

- [org_policy_compiler.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/org_policy_compiler.py)

## 4. Lane Event -> Recall Pipeline

`gr2` emits neutral lane events into:

- `.grip/events/lane_events.jsonl`

Example event:

```json
{
  "type": "lane_enter",
  "agent": "agent:atlas",
  "agent_id": "agent_atlas_ghi789",
  "owner_unit": "design-research",
  "lane": "auth-refactor",
  "lane_type": "feature",
  "repos": ["grip", "premium"],
  "timestamp": "2026-04-12T14:06:45+00:00",
  "event_id": "47db552da9a1535c"
}
```

Recall consumes these events without importing Premium semantics.

### Indexing Surface

The recall prototype indexes the event log:

- by lane
- by actor
- by repo
- by time range

### Query Interface

Examples:

- `lane_history("auth-refactor")`
- `actor_history("agent:atlas")`
- `repo_activity("grip")`
- `time_range(start, end)`

These support queries like:

- “what happened on the auth-refactor lane last week?”
- “what lanes did atlas touch?”
- “who last worked in grip?”

Living prototype:

- [recall_lane_history.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/recall_lane_history.py)

## 5. Channel Bridge

Lane events also feed a channel bridge.

### Recommended Model

The recommended model is watcher-first, not synchronous posting.

Watcher flow:

```text
lane_events.jsonl
    -> watcher cursor
    -> channel outbox
    -> channel transport
```

Why watcher-first:

- lane transitions remain durable even if channel delivery is down
- replay is resumable from the append-only event log
- dedupe is explicit through `event_id`
- channel posting does not block lane transitions

### Outbox Format

The bridge produces channel-compatible rows such as:

```json
{
  "type": "channel_post",
  "channel": "#dev",
  "delivery": "watcher",
  "source_event_id": "47db552da9a1535c",
  "source_event_type": "lane_enter",
  "agent": "agent:atlas",
  "agent_id": "agent_atlas_ghi789",
  "owner_unit": "design-research",
  "lane": "auth-refactor",
  "lane_type": "feature",
  "repos": ["grip", "premium"],
  "message": "agent:atlas entered design-research/auth-refactor [feature] repos=grip,premium",
  "timestamp": "2026-04-12T14:06:47+00:00"
}
```

The watcher keeps cursor state in:

- `.grip/events/channel_bridge.cursor.json`

and writes outbox rows to:

- `.grip/events/channel_outbox.jsonl`

Living prototype:

- [channel_lane_bridge.py](/Users/layne/Development/synapt-codex/atlas-gr2-playground-stack/gr2/prototypes/channel_lane_bridge.py)

## 6. Lane Lifecycle Invariants

The lane model needs three additional invariants to survive real
human/agent collaboration.

### 6.1 Handoff Uses Continuation Lanes

Agent-to-agent relay should not let the target agent execute inside the source
unit's lane root.

Rules:

- cross-unit shared working lanes are not the handoff model
- handoff creates a continuation lane under the target unit
- continuation lanes preserve source linkage, but give the target unit:
  - its own lane root
  - its own lease scope
  - its own current-lane state

Implication:

- handoff preserves the unit-scoping invariant
- shared cross-unit lane execution does not

### 6.2 Identity Rebinding Freezes And Continues

When Premium recompiles an agent from one unit to another while live lanes
exist:

- old lanes stay where they are
- old lanes become frozen
- active leases under the old unit are force-released
- old-unit exec planning is blocked
- resumption happens through continuation lanes under the new unit

The minimal contract gr2 needs from Premium is:

- same `agent_id` continuity
- explicit `old_owner_unit -> new_owner_unit` mapping
- `pending_reassignment` hint recommended

Implication:

- gr2 does not silently move or rename active lane roots
- rebind is a freeze-and-relay flow, not a mutation-in-place flow

### 6.3 Workspace Constraints Are Enforced Locally

Premium compiles workspace-wide constraints into the spec. gr2 enforces the
compiled result without importing org logic.

The current prototype proves two critical cases:

- `max_concurrent_edit_leases_global`
  - enforced across all units in the workspace
  - a third edit lease is blocked once the workspace cap is reached
  - force-breaking a stale local lease does not bypass the global cap
- `required_reviewers`
  - evaluated per repo and PR from review-lane state
  - `check-review-requirements` reports satisfied vs unsatisfied based on the
    compiled count

Implication:

- workspace-wide coordination rules can remain Premium-owned in definition
- OSS can still enforce the compiled constraint deterministically

## 7. Premium Boundary Rules

These rules should remain hard.

### Must Stay In Premium

- persistent agent identity
- org membership
- role evaluation
- entitlement evaluation
- workspace assignment
- reassignment history
- policy compilation
- reviewer policy semantics
- degradation policy for loss of premium

### Can Live In OSS

- workspace-scoped unit records
- lane metadata
- lease enforcement
- workspace-wide constraint enforcement from compiled spec
- review-requirement satisfaction checks from compiled spec
- lane events
- event indexing
- channel outbox derivation
- local execution planning
- local materialization of compiled constraints

### Must Not Happen

- `gr2` must not decide who an agent is
- `gr2` must not resolve org role semantics
- `gr2` must not invent workspace policy not present in the compiled spec
- recall must not require Premium logic to answer lane-history queries
- channel bridge must not depend on synchronous control-plane availability

## Prototype References

Living examples for this integration layer:

- [identity_unit_binding.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/identity_unit_binding.py)
- [org_policy_compiler.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/org_policy_compiler.py)
- [recall_lane_history.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/recall_lane_history.py)
- [lane_workspace_prototype.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/lane_workspace_prototype.py)
- [cross_mode_lane_stress.py](/Users/layne/Development/synapt-codex/atlas-gr2-identity-org/gr2/prototypes/cross_mode_lane_stress.py)
- [channel_lane_bridge.py](/Users/layne/Development/synapt-codex/atlas-gr2-playground-stack/gr2/prototypes/channel_lane_bridge.py)

These prototypes are still part of the loop:

- `(design -> prototype -> verify)^n`
- `build`
- `assess`
- `repeat`

They should remain the seam-definition reference until the production
implementation lands.
