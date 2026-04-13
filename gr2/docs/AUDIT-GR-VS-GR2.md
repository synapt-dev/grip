# Audit: `gr` vs `gr2`

This document compares the current shipped `gr` surface against the current
`gr2` surfaces.

The goal is not to declare parity prematurely. The goal is to identify:

- what `gr` already does in production
- what `gr2` already proves
- what remains missing before `gr2` can replace `gr`
- which runtime is authoritative during the transition

## 1. Current Roles

### `gr`

`gr` is the current production multi-repo tool.

It is broad, mature, and still required for daily workflow coverage across:

- repo bootstrap and migration
- cross-repo git workflow
- PR and issue workflow
- agent orchestration
- channel integration
- release and CI operations

### `gr2`

`gr2` has the better long-term architecture, but today it is split across two
surfaces:

- Rust `gr2`
  - registry/spec/plan/apply backbone
- Python `gr2`
  - active UX proving layer for lanes, hooks, workspace bootstrap, and real git behavior

## 2. Transition Decision

This is the operating rule for the project:

- Python `gr2` is the active UX authority now.
- Rust `gr2` is the future backend/runtime replacement.

That means:

- new user-facing command design should happen in Python first
- Python command names, config shapes, and event schemas are the contract
- Rust should reimplement the proven contract later, not invent a second UX

## 3. Command Matrix

Status meanings:

- `Production` — usable in `gr` today
- `Shipped (Python)` — available in Python `gr2`
- `Shipped (Rust)` — available in Rust `gr2`
- `Partial` — exists but does not yet cover the full workflow
- `Missing` — no real replacement yet

| Workflow | `gr` | Python `gr2` | Rust `gr2` | Audit |
|---|---|---|---|---|
| Workspace bootstrap from existing repos | `gr init --from-dirs`, `gr migrate ...` | `workspace init` | `init` only creates empty workspace | `gr2` partial |
| Workspace materialization from spec | `gr sync` / manifest flow | `workspace materialize` | `apply` | `gr2` partial, split across runtimes |
| Show repo maintenance state | `status` | `repo status` | `repo status` | `gr2` shipped |
| Inspect repo hook config | no first-class equivalent | `repo hooks` | missing | `gr2` Python-only |
| Create task context / lane | no first-class lane model | `lane create` | `lane create` | `gr2` shipped |
| Enter active task context | no first-class lane enter | `lane enter` | missing | Python-only |
| Exit active task context | no first-class lane exit | `lane exit` | missing | Python-only |
| Recover current context | ad hoc (`gr status`, branch state) | `lane current` | missing | Python-only |
| Lease/occupancy control | no first-class equivalent | `lane lease acquire/release/show` | missing | Python-only |
| Lane-aware execution planning | indirect | missing in CLI, prototypes exist | `exec status` | split/incomplete |
| Lane-aware execution run | indirect | missing | missing | missing |
| Review requirement check | indirect/manual | `review requirements` | missing | Python-only |
| Declarative spec show/validate | limited via manifest | missing | `spec show`, `spec validate` | Rust-only |
| Plan workspace drift | implicit in sync/status | missing | `plan` | Rust-only |
| Apply workspace drift | `sync` | missing as explicit command | `apply` | Rust-only |
| Real git lane checkout creation | no lane model | shipped | missing | Python-only |
| Hook-driven file projections | `link`/manifest model | shipped | missing | Python-only |
| Hook-driven lifecycle (`on_materialize`, `on_enter`, `on_exit`) | ad hoc scripts | shipped | missing | Python-only |
| Branch create/switch across repos | `branch`, `checkout` | partial via lane branch intent + git checkout in lane flow | missing | `gr` still primary |
| Stage / restore / diff / commit / push | shipped | missing | missing | `gr` only |
| PR create / merge / review / checks | shipped | missing | missing | `gr` only |
| Issue workflow | shipped | missing | missing | `gr` only |
| Group / target / cache / gc | shipped | missing | partial (`repo status` sees cache-style model only) | `gr` only |
| Tree / griptree workflow | shipped | missing | missing | `gr` only |
| Spawn / dashboard / channel | shipped | missing from CLI, only prototype seam docs | missing | `gr` only |
| Release / CI / bench / verify | shipped | missing | missing | `gr` only |

## 4. What `gr2` Already Proves

Even though `gr2` is not feature-complete, it already proves the more coherent
workspace model:

- lanes are the primary working surface
- leases make occupancy explicit
- review requirements can be enforced from compiled constraints
- hooks travel with repos via `.gr2/hooks.toml`
- workspace bootstrap and materialization can be expressed as a clean spec flow
- real git worktrees/checkouts can be created and managed from the lane model

This is the architectural advantage over `gr`.

## 5. What Still Keeps `gr` Necessary

Today the team still needs `gr` for normal production work because `gr2` does
not yet cover:

- daily cross-repo branch / commit / push flows
- PR creation / merge / review workflow
- issue workflow
- tree/griptree lifecycle
- agent spawn / mission control / channel operations
- release/CI surfaces

The practical result is:

- `gr` remains the production multitool
- `gr2` remains the proving path for the replacement model

## 6. Biggest Current Problem

The biggest current `gr2` problem is not missing ideas. It is split authority.

Right now:

- Rust `gr2` owns spec/plan/apply/registry concepts
- Python `gr2` owns the best real UX and real hook/git behavior

That is acceptable during transition, but not as a steady state.

## 7. Recommended Structure

### Near-term

- Python `gr2` defines the user-facing interface
- Rust `gr2` should not grow competing UX nouns
- use Python to prove:
  - command names
  - hook schema
  - lane semantics
  - workspace bootstrap/materialization behavior

### Mid-term

- port proven Python commands behind the same interface into Rust
- keep the Python CLI as the compatibility oracle during the port

### Long-term

- Rust becomes the backend/runtime implementation
- Python stops being the primary runtime, but not before command parity exists

## 8. Replacement Rule

`gr2` does not replace `gr` when it matches one subsystem.

`gr2` replaces `gr` only when one coherent `gr2` surface can cover:

- workspace init/materialize
- repo status/hooks
- lane create/enter/exit/current/lease
- spec/plan/apply
- review requirements
- the minimum daily repo workflow needed by the team

Until then, we should be explicit:

- use `gr` for broad production workflow
- use Python `gr2` to prove the replacement model

## 9. Immediate Next Steps

1. Keep Python `gr2` as the UX authority.
2. Add missing daily-workflow surfaces in Python before porting them.
3. Avoid adding overlapping user-facing Rust commands unless they are direct ports.
4. Maintain this matrix as the transition scoreboard.
