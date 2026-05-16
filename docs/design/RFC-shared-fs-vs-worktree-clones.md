# RFC: Shared-FS Gripspace Root vs Per-Agent Clones

**Issue**: grip#717
**Author**: Apollo
**Date**: 2026-05-07
**Status**: Draft / RFC
**Type**: Architectural design decision

## Problem

The current gripspace layout is an unnamed hybrid. `config/` lives at the gripspace root as a shared filesystem directory visible to all agents. But each agent's worktree (`synapt-dev/`, `synapt-global/`, `synapt-codex/`) contains its own clone of every repo, including config. Files written to `~/Development/synapt/config/` are visible to all agents via shared FS, but commits happen only in agent-worktree-local clones.

This causes a copy-step friction: Sentinel hit it on 2026-05-07 when shipping B2. He wrote a design doc to the shared config path, then had to copy it into his worktree-local config clone before he could `git add` and commit. Without the copy, the file existed on the filesystem but was outside his clone's git index.

Every cross-worktree config edit requires this copy step. Agents new to the pattern discover it by failing.

## Substrate Context

Three prior architectural decisions constrain the solution space.

### 1. Puddle-Orchestra (Sprint 5)

"The worktree model failed because it optimized for the wrong thing. Worktrees save disk space and clone time. But the real cost in a multi-agent system isn't disk; it's state contamination. When agents share a .git directory, they share reflog, branch state." Per-agent isolation is a feature, not an accident. The system is oceans-of-puddles: each agent is a self-contained puddle with its own state. Orchestration happens between puddles, not within a shared pool.

**Implication**: Any solution that merges per-agent git state (shared `.git/`, shared reflog) reintroduces the state contamination the puddle model was designed to eliminate.

### 2. Grip Object Model (grip#473)

gr2 uses git plumbing as a content-addressable database. Grip commits are workspace snapshots. Agent workspaces are full clones, not linked worktrees. The four-sync decomposition (topology sync, content sync, materialization sync, operational sync) treats each sync layer independently. Worktree-isolation is not fighting against grip's vision; it IS grip's vision.

**Implication**: The isolation model is load-bearing. It's not a convenience choice; it's the foundation that the sync decomposition builds on.

### 3. Hot/Cold Architecture (Synaptsis)

The WAL/checkpoint pattern: filesystem = hot (mutable, fast writes); commits = cold (immutable, durable snapshots). Shared-FS-without-shared-clone is a hot-without-cold mismatch. Files exist in the hot layer (visible on disk) but have no path to the cold layer (commits) without a manual copy step. This is the structural source of the friction.

**Implication**: The fix must either (a) give the hot layer a direct path to cold, or (b) make the hot layer explicitly per-agent so there's no expectation of cross-agent visibility.

## Options

### Option A: Single Shared Clone for Config

All agents share one config clone at the gripspace root. Edits by any agent are reflected in the same git working tree. Commits are coordinated via branch discipline.

**Pros**:
- Eliminates the copy step entirely
- Simplest mental model: one directory, one git index
- Config changes are immediately visible to all agents

**Cons**:
- Reintroduces shared `.git/` state for config. Two agents committing concurrently must coordinate: index locks, branch state, staging conflicts
- Breaks the puddle-orchestra model for this one repo. Config becomes a shared pool, not a puddle
- Branch discipline becomes critical. If agent A is on `sprint-33` and agent B is on `feat/x`, the shared working tree can only be on one branch at a time
- Griptree machinery (`gr tree add`) creates per-branch worktrees. A shared clone cannot participate in griptrees without forking the model

**Verdict**: Architecturally inconsistent. Solves the symptom (copy step) by reintroducing the disease (shared state). The puddle model rejected this for code repos for good reason; the same reasons apply to config.

### Option B: Per-Agent Clones, No Shared FS

Each agent has their own config clone. There is no shared config directory at the gripspace root. Cross-agent config coordination happens via push/fetch, the same as code repos.

**Pros**:
- Clean isolation. Each agent's config is a puddle
- Consistent with the grip object model: every repo is treated the same way
- No copy step because there's no ambiguous shared path
- Griptrees work naturally (per-agent branches on config, just like code)

**Cons**:
- Cross-agent latency. If Sentinel writes a design doc, Opus sees it only after push + fetch/sync
- Loses the convenience of "drop a file in shared config and everyone sees it"
- Requires discipline: agents must push config changes promptly. Local-only config edits become invisible

**Verdict**: Architecturally consistent but removes a real convenience. The shared-FS visibility of config is genuinely useful for coordination artifacts (design docs, sprint plans, process docs). Losing it entirely adds push/fetch latency to every coordination edit.

### Option C: Documented Hybrid + Helper Tooling

Keep the current layout. Document the pattern explicitly. Add a `gr sync-file` (or similar) helper that copies shared-FS edits into the agent's worktree-local clone before commit.

**Pros**:
- Minimal change. No architectural disruption
- The copy step becomes a one-command operation instead of manual discovery
- Preserves shared-FS convenience for reading

**Cons**:
- Band-aid over architectural ambiguity. The unnamed hybrid remains unnamed
- New agents still need to learn the pattern. The helper reduces friction but doesn't eliminate the conceptual overhead
- Two sources of truth: the shared-FS version and the worktree-local version can drift if the helper isn't used

**Verdict**: Pragmatic but unsatisfying. Reduces friction without resolving the structural mismatch. Acceptable as a short-term patch; not a long-term answer.

### Option D: Semantic Split (Recommended)

Split by semantic role. **Coordination state** (config repo: agent prompts, process docs, sprint plans, skills) gets a shared clone at the gripspace root. **Code repos** (recall, premium, grip, site) remain per-agent clones.

This is the control-plane-vs-data-plane split:
- **Control plane** (config): shared, read-mostly, low-contention. Edits are infrequent and coordination-oriented. Shared clone is safe because write contention is low and the content is inherently shared
- **Data plane** (code repos): per-agent, high-contention, isolation-critical. The puddle model applies in full

**Pros**:
- Eliminates the copy step for config (the actual friction source)
- Preserves per-agent isolation for code repos (the actual contamination risk)
- Matches the real contention profile: config has low write concurrency; code has high write concurrency
- The hot/cold mismatch is resolved for config: shared FS (hot) maps to shared clone (cold)

**Cons**:
- Two models in one gripspace. Config is shared; code is isolated. Agents must know which is which
- Config contention, while low, is not zero. Two agents modifying the same process doc concurrently can conflict. Branch discipline is still needed for config
- Griptree machinery would need to handle config differently (shared clone doesn't get per-agent worktrees, or gets them optionally)

**Mitigation for cons**:
- The two-model distinction maps to a real semantic difference (coordination vs work). Agents already treat config differently from code: they read config for instructions, they write code for output
- Config contention can be managed with lightweight conventions: claim config edits in #dev before starting, merge config PRs promptly. The shared-file-coordination retro item from Sprint 29 already established this pattern
- Griptree handling: config participates in griptrees only when explicitly branched (e.g., sprint branch changes to agent prompts). Default behavior: config stays on main in all griptrees

## Recommendation

**Option D: Semantic split.** The control-plane-vs-data-plane distinction is not an arbitrary line; it reflects the actual write-contention profile and coordination semantics of the repos. Config is shared state that all agents read and occasionally write. Code is per-agent work product with high write concurrency.

The puddle-orchestra model is correct for code: isolation prevents state contamination. But config is not a puddle; it's the water table that all puddles draw from. Treating it as a puddle (Option B) adds latency to coordination. Treating everything as a shared pool (Option A) reintroduces contamination. The semantic split (Option D) gives each category the model that fits its actual usage pattern.

### Section 5: Branch Policy for Shared Repos

A shared clone's working tree can only be on one branch at a time. Without explicit branch policy, Option D inherits a different shape of shared-state contamination: two agents on different branches can't both have their changes visible simultaneously. This section makes branch management a first-class design decision, not an implementation footnote.

**Default branch**: shared repos stay on `main`. All edits land via PR, never direct commits to main.

**Sprint branches**: when prompts, process docs, or eval artifacts need sprint-local evolution, config participates in the sprint branch. This requires coordinator-level signoff (Opus or sprint lead) and short-lived branching. The sprint branch merges back to main at ceremony, same as code repos.

**Concurrent branch conflicts**: if two agents need incompatible config-branch state at the same time (e.g., one agent testing new prompts on `feat/x` while another needs current main prompts), the requesting agent creates a temporary per-agent config worktree (`gr tree add feat/x` on config only). This is the escape hatch, not the default.

**Claim-before-edit convention**: the Sprint 29 retro item ("coordinate shared file edits in #dev before starting") becomes load-bearing for shared repos. Before modifying any file in a shared clone, claim it in #dev. This was advisory before; under Option D it is mandatory.

**Rollback path**: if the shared clone gets into a conflicted state, any agent can `git stash` + `git checkout main` to restore clean state. The stashed changes are not lost; they go through the PR path after the conflict is resolved.

### Repo Classification

Based on review feedback, the binary control-plane/data-plane distinction holds with one edge case:

| Repo | Classification | Rationale |
|------|---------------|-----------|
| **config** | Control-plane (shared) | Coordination state: prompts, process, sprint plans |
| **recall** | Data-plane (per-agent) | Code with high write concurrency |
| **premium** | Data-plane (per-agent) | Code with high write concurrency |
| **grip** | Data-plane (per-agent) | Code with high write concurrency |
| **site** | Data-plane (per-agent) | Mixed (blog content + Eleventy build). Lean code due to build/dev workflow |
| **codememo-benchmark** | Control-plane (shared) | Low edit frequency, pinned versions |
| **homebrew-tap** | Control-plane (shared) | Low edit frequency, multi-agent |

`site` is the only edge case. It has coordination content (blog posts, brand assets) but also a real build system and PR workflow. Classification as data-plane keeps the line clean; Option C helper tooling can address the occasional shared-content-edit friction for blog posts.

### Manifest Flag Design

Per review consensus: use an explicit `shared: true` manifest flag for now, not semantic derivation. Premature taxonomy locks in abstractions before the team has empirical signal on edge cases. The flag gives evolutionary flexibility; semantic role enrichment can come in a future sprint after we've seen which repos genuinely sit in the gray zone.

### Implementation Sketch (Future Sprint)

If this RFC is accepted, implementation would involve:

1. **Gripspace manifest change**: mark repos with a `shared: true` flag (or a `role: control-plane` semantic)
2. **`gr init` / `gr tree add` behavior**: shared repos get one clone at the gripspace root, not per-agent copies. Per-agent repos get individual clones as today
3. **`gr sync` behavior**: shared repos sync in-place at the gripspace root. Per-agent repos sync within each agent's workspace
4. **Convention doc**: update CLAUDE.md and agents.md to document the two-model pattern. Config = shared, commit via branch discipline. Code = isolated, commit freely
5. **Migration path**: for existing gripspaces, `gr migrate` moves config from per-agent clones to shared root (or vice versa)

### Risk: Scope Creep

This RFC is a design decision, not a mandate for immediate implementation. The current friction (copy step) is real but low-frequency. If the team decides the implementation cost exceeds the friction cost, Option C (helper tooling) is an acceptable intermediate step that can coexist with a future Option D migration.

## Review Status

**Sentinel**: directionally green. Tightened with branch-policy addition (Section 5). Site = data-plane. Manifest flag should be tied to explicit semantic role.

**Opus**: directionally green. Echoed Sentinel's branch-policy tightening. Answered all 4 original questions. Manifest flag now, semantic role later.

## Decision Requested

Questions resolved and open:

1. **Repo classification**: resolved. Control-plane/data-plane holds. Site = data-plane (edge case, handled with helper tooling). See Repo Classification table above.
2. **Config write-contention**: reframed per Opus review. Low *conflict rate*, moderate *write concurrency*. Viable with explicit branch policy (Section 5).
3. **Manifest flag vs semantic role**: author decision: **manifest flag for now**. Sentinel argued for semantic role from the start (prevents unbounded boolean drift). Opus argued for flag now, role later (avoid premature taxonomy). Both defensible. Choosing flag because empirical signal on edge cases (which repos are genuinely gray-zone) is insufficient to design a stable taxonomy today. If the flag proliferates beyond 2-3 repos, revisit with semantic role enrichment. Noting the disagreement here so the team can revisit if needed.
4. **Timeline**: Sprint 34 candidate, not blocking. Friction is real (hit Sentinel on B2, hit Opus earlier the same session). Implementation cost is medium (~1 week). Gate on competing priorities.
5. **Branch policy for shared repos** (added per Sentinel review): see Section 5 above. Default = main, edits via PR, sprint branches with coordinator signoff, claim-before-edit mandatory.
