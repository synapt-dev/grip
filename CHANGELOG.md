# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.2] - 2026-09-12

**Scope.** This release tags the `release/1.x` patch branch tip. The promoted range is
`v1.5.1..bf080578` (the immutable `release/1.x` tip at freeze, not the moving ref) —
**5 commits, 2 merge commits, and 2 first-parent units**, measured with `git rev-list
--count`, `git rev-list --merges --count`, and `git rev-list --first-parent --count` over
that exact range, with the empty range `origin/release/1.x..v1.5.1` returning 0 as the
control (tag `v1.5.1` = annotated `cabe3c6f`, dereferencing to commit `f24fab47`). The
release-prep commit carrying this entry sits one beyond that range and is excluded. Rust
source changed since 1.5.1 — `src/cli/commands/spawn.rs`, `src/cli/args.rs`,
`src/cli/dispatch.rs`, and a new `tests/spawn_interactive.rs` — so this is a substantive
patch release, not a metadata-only bump.

**At the tag.** `v1.5.2` sits on this PR's merge into `release/1.x`, two commits beyond
`bf080578` (the bump commit and its merge), so a reader measuring `v1.5.1..v1.5.2` gets
**7 commits / 3 merges**: the promoted range plus this bump and its merge. The
promoted-range counts above are the substantive figure.

**gr spawn interactive foreground fallback (#1067).** `gr spawn up` now falls back to an
interactive foreground launch when no multiplexer (tmux) is present, instead of failing.
The fallback is documented in `docs/gr-spawn.md`, and `tests/spawn_interactive.rs` pins the
behavior; a `build_agent_launch` return type is named for the Clippy lint.

**CI/release for the patch branch (#1063).** The tag-pattern-split `release.yml` is carried
onto the `release/1.x` branch so a `v1.x.y` tag runs ci → build → release-gr1 →
publish-crates, and `release/*` self-hosts CI so a PR onto this branch reports the required
context.

## [1.5.1] - 2026-09-10

**Scope.** This release promotes `v1.5.0..<dev tip>` — **15 commits, 7 merge commits, and 7
first-parent units**, measured with `git rev-list --count`, `git rev-list --merges --count`, and
`git rev-list --first-parent --count` over `v1.5.0..origin/dev` (tag `v1.5.0` = 9894260d), with the
empty range `v1.5.0..v1.5.0` returning 0 as the control. The release-prep commit carrying this
entry sits one beyond that range and is excluded; this bump's own merge into `dev` and the
`dev`→`main` promote merge add to a tag-range figure a reader measuring `v1.5.0..v1.5.1` would see.
Rust source changed since 1.5.0 — three source files (`src/cli/commands/spawn.rs`,
`src/cli/dispatch.rs`, `src/core/gripspace.rs`) and two test files
(`tests/spawn_callsite_argorder.rs`, `tests/gripspace_include_rev_dirty.rs`) — so this is a
substantive patch release, not a metadata-only bump.

**gr2 review-run hardening (#1050, #1051).** A review lane can now run more than once: the run log
is allow-listed so `review run` no longer refuses the second run as untracked drift, and a run
refused before pytest writes a refusal receipt so `close-gr` can reclaim the lane. Import resolution
is isolated on two axes — `-I` keeps the lane's cwd from shadowing its own package as a namespace
directory, and a scrubbed environment (every `PYTHON*` variable dropped) is shared by the import
checks and the pytest run, so a rogue package on `PYTHONPATH` can neither fool the check nor be
imported by the run while the check certifies the lane.

**gr spawn arg ordering (#1046, #1047, #1048).** `gr spawn up` now applies tool-level args to the
launch command, with a call-site witness pinning that tool args compose before agent args; the
witness is gated to `cfg(unix)` so it does not red the Windows build.

**gripspace includes (#1045).** A rev-pinned gripspace-include clone that is dirty no longer drops
its included repos: the resolver keeps them instead of silently taking the shorter list.

**CI (#1049).** The clippy doc-comment lint on `assemble_launch_parts` is cleared and the Windows
job is required in the CI aggregate, so the aggregate context that gates merges cannot go green
without Windows.

## [1.5.0] - 2026-09-07

**Scope.** This release promotes `v1.4.0..12b08f1e` — the immutable sha `12b08f1e` (the
`dev` tip at freeze), not the moving `dev` ref: **70 commits, 35 merge commits, and 8
first-parent units**, measured with `git rev-list --count`, `git rev-list --merges
--count`, and `git rev-list --first-parent --count` over that exact range, with the empty
range `v1.4.0..v1.4.0` returning 0 as the control. The release-prep commit carrying this
entry sits one beyond that range and is excluded.

**At the tag.** The release tag `v1.5.0` sits three commits beyond `12b08f1e`, so a reader
measuring `v1.4.0..v1.5.0` gets **73 commits / 37 merges**: the promoted range plus this
bump commit, its merge into `dev`, and the `dev`→`main` promote merge. The promoted-range
counts above are the substantive figure; the tag-range figure is stated so a reader who
recomputes at the tag is not surprised.

**Why 8 first-parent units expand to 70 commits.** The first-parent line is short because
unit 1 is a promote merge (#1035, `promote/dev-to-main-review-verbs`, now also `main`'s
tip) that folds the entire review-verb, review-flow, `prune`, `target`, fixes, and CI
stack — the 27 PRs in the `#1004`..`#1034` span, which were 27 first-parent units before
the promote collapsed them — onto its second-parent side. The remaining 7 first-parent
units are the post-promote gr2 packaging and review-run fixes, #1036–#1042. So the work
below is enumerated by PR, not by first-parent unit; the two diverge here by design.

**Zero of the 92 changed files touch the published Rust CLI (`src/`)**, and neither
`Cargo.toml` nor `Cargo.lock` changed in the range — measured with `git diff --name-only
v1.4.0..12b08f1e -- src/ Cargo.toml Cargo.lock` (0 files) and by directory (85 under `gr2/`, 2 under
`.github/`, 2 under `scripts/`, 2 in the repo root, 1 under `tests/`). The one Rust file
that changed, `tests/release_pipeline_contract.rs`, is a test-only integration test and
does not enter the compiled binary. So the Rust binary published to crates.io at the tag
is **byte-identical to 1.4.0**; this is the first grip release that changes no shipped
Rust code. The minor bump reflects new user-facing verbs in the gr2 overlay, not a change
to the crate. (This bump commit itself is the only edit to `Cargo.toml`/`Cargo.lock`.)

### Carried by the promote merge (#1035 — PRs #1004–#1034)

**The three review verbs (the R2 "Exact Work" closing fruit).** A frozen-range Python
review through gr2 now runs end to end with no raw-shell exit point:
- `review bind --from-range` (#1032) ingests a frozen `range.patch` directly.
- `review close-gr` (#1033) is a verb-owned teardown for an `open-gr --enter` lane.
- `review run` (#1034) folds the reviewer's in-lane `venv` + install + `pytest` into one
  verb, with a two-sided tree binding (tracked-tree equality plus an untracked-drift scan)
  and an import-under-lane check, so a green is always about the bound tree; counts come
  from pytest's summary line and a green requires `passed >= 1`.

**Review-flow infrastructure (10 PRs).** Receipt emitter (#1008); pre-push head
materialization via the blobless+sparse path (#1009); reconstruct a pre-push head from a
carried range (#1010); committer-faithful reconstruction so the reconstructed sha equals
the pinned head (#1011); run reviewed tests inside the lane, recorded in the receipt
(#1017); multi-repo verification from per-repo spec commands (#1019); provision the lane's
own venv (#1020); end-to-end carry-range reconstruction test (#1022); install declared
test-extras into the lane venv (#1023); print the refusal reason on default output (#1031).

**prune verb stack (4 PRs).** `prune` — merged-branch cleanup by patch-id + squash,
never containment (#1025); default target resolves dev before origin/HEAD, not silently
main (#1026); help text (#1027); prefer the gripspace stored `[settings].target` (#1028).

**target verb (2 PRs).** `target set/show/unset` — the writer for the stored PR target
prune reads (#1029); atomicity witness — the original spec survives a failed replace (#1030).

**Fixes (5 PRs).** Lane create records the fork base so review create-project works
(#1004); workspace status flags a hand-made linked worktree (#1006); pin pr-merge `--json`
assertions to stdout across click versions (#1015); record lane fork_base even when a
projection hook blocks (#1016); pin that source-transport does not change review identity
(#1021).

**CI and test scaffolding (3 PRs).** Raise the Windows test-job timeout 15→30 (#1005);
guard against a `.pyc` with no source (#1007); lift the version-safe CliRunner shim into
one shared helper (#1018).

### Post-promote fixes (first-parent units #1036–#1042)

- **rmtree teardown refuses on partial failure (#1036).** A lane/review teardown that
  cannot fully remove its tree refuses rather than leaving a half-deleted directory;
  witnessed by `test_rmtree_or_refuse.py`.
- **`review run` install hint (#1037).** A reviewer whose in-lane install is missing an
  extra now gets a printed `.review-install` hint instead of a bare failure.
- **`review run` undeclared-extra guard (#1038).** An extra named in `.review-install`
  but not declared by the project is caught before the install runs.
- **gr2 overlay import-package rename (#1039).** `gr2_overlay` → `gr2/overlay`, so the
  installed import package matches the distribution layout.
- **gr2 packaging (#1040).** A PEP 517 build backend that sources the version from
  `Cargo.toml` (`gr2/_build/version_from_cargo.py`), a `MANIFEST.in`, and release-workflow
  wiring to build and publish the overlay.
- **`review run` doors (#1041).** Entry/exit guarding so `review run` and `close-gr`
  refuse to act outside a bound lane; `test_review_run.py` + `test_review_close_gr.py`.
- **release-pipeline contract: stale `needs:` (#1042).** The Rust contract test asserts
  every publish job declares `needs: ci`, closing the gap the standing-stop lift opened.

**Not in this range.** No spawn or graceful-shutdown fix landed in `v1.4.0..dev`.

## [1.4.0] - 2026-09-05

**Scope.** This release promotes `v1.3.1..dde8c816`: **144 commits and 35 first-parent
units**, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range, with the empty range `v1.3.1..v1.3.1` returning 0 as the control.
The release-prep commit carrying this entry sits one beyond that range and is excluded.

Units are classified by diffing each against its **first parent** (a merge commit has no
canonical diff, and `--name-only` returns nothing on a merge). **Five of the 35 units
touch the published Rust CLI (`src/`)**: gripspace-name validation for Windows path
separators, two MCP cancellation fixes (a stdout leak and a concurrent-cancel class), the
refusal of `clone_strategy: worktree`, and the CI-green catch-up promotion. The remaining
30 are the gr2 Python overlay and CI/release-pipeline hardening, which do not change the
shipped Rust binary.

**gr2 overlay (the bulk of this release).** The R2 "Exact Work" review chain: project
review over an immutable multi-repo pin, `review bind`/`open-gr` reconstruction with a
tree-equality assertion, the `review create-project` producer verb and the materialize
path (`open-project --enter`), and review-ephemeral lanes cloned blobless + sparse from a
persistent per-host mirror. The fork-base ruling: a review's per-repo base is the recorded
fork point, never `HEAD^`. Lane lifecycle safety, lane-aware commit, workspace kinds, and
`workspace convert-clone` with a no-worktree materialization playbook.

**Release pipeline.** `release.yml` now gates on CI: `build` needs `ci`, and both
`release` and `publish-crates` need `[ci, build]`; `cargo publish` runs `--locked` with
`--no-verify` removed. CI runs on `dev`, with no-fail-fast reporting.

## [1.3.1] - 2026-08-30

**Scope.** This release promotes `v1.3.0..3a8df911`: **22 commits and 5 first-parent
units**, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range, with the empty range `v1.3.0..v1.3.0` returning 0 as the control.
The release-prep commit carrying this entry sits one beyond that range and is excluded.

Units are classified by diffing each against its **first parent**, because a merge commit
has no canonical diff. **Four of the five units change the published Rust CLI**
(`src/mcp/server.rs`, `src/cli/commands/push.rs`, `src/cli/commands/pr/create.rs`,
`src/cli/commands/pr/merge.rs`); the fifth touches `tests/` only and changes no shipped
behaviour. There are no gr2 units in this range.

Every entry below is a correction to a command that previously reported one thing and did
another. None adds a capability.

### Fixed
- **`gr pr create` and `gr pr merge` resolve the base from the request and the platform**,
  not from the manifest's stored target. A stale stored target silently opened and merged
  PRs against the wrong base; an unreadable base ref is now reported instead of silently
  clearing the merge.
- **`gr pr` exit status agrees with the failure it just printed.** The command could print a
  platform failure and still exit 0, so a caller reading `$?` and a caller reading the
  output reached opposite conclusions. `--json` now keeps exit 0 by design and carries
  pass/fail in the payload's `success` field, which is set at the production call site
  rather than only in the serialization helper.
- **`gr push` names every repository it actually pushed**, with its ref and commit. The
  summary previously named only the repositories with nothing to push, so the operator saw
  the inverse of what happened; a repository with nothing to push is not listed.
- **The gr1 MCP server speaks newline-delimited stdio**, matching the transport its clients
  use.

### Internal
- MCP harness response reads are bounded, so a harness that never receives a reply fails
  with a timeout instead of hanging. Tests only; no shipped behaviour changes.

## [1.3.0] - 2026-08-25

**Scope.** This release promotes `v1.2.0..c095d5d`: 6 commits and 3 first-parent
units, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range. All three units change the published Rust CLI.

### Added
- **Codex startup prompts use the native developer boundary.** `gr spawn` loads each
  configured `startup_prompt` and supplies it as `developer_instructions`, while recall
  continuity remains a separate optional user payload. Missing configured prompts fail
  closed. Generated launch scripts are constructed on mode-0600 inodes and atomically
  replace prior files.

### Fixed
- **Unknown repository filters are refused** by `gr add`, `gr commit`, and `gr push`
  instead of matching zero repositories and reporting success.
- **Pruning protects the remote default branch** in addition to the current and target
  branches.

## [1.2.0] - 2026-08-21

**Scope, stated first because the range and the artifact are not the same thing.**

The work promoted in this release is the range **`v1.1.0..6192e8c5`** — from the 1.1.0 tag to the
`dev` tip at freeze — which is **42 commits / 14 first-parent units**. The release-prep commit
that carries this entry sits one beyond that and is deliberately excluded; counting it makes the
range 43 / 15. Both numbers are true of different ranges, so the range is named rather than left
to inference.

Units are classified by diffing each against its **first parent**. A merge commit has no
canonical diff, and `git show <merge> --name-only` returns nothing for these, so the method is
part of the claim.

**Five of those 14 units are in the published `gitgrip` crate; nine are not.** gr2 is a separate
Python surface at version `0.1.0` that is not distributed, so its nine units — the propagation
prototypes, the append/torn-line work, the native daily verbs, and workspace-spec-from-topology —
are present in this repository and absent from anything `cargo install gitgrip` gives you. The
entries below cover the five gr1 units only. Read the git range if you want the gr2 work.

### Fixed
- **`gr pr merge` and `gr checkout` exit nonzero when part of a batch fails** — a multi-repo
  operation that failed in some repos and succeeded in others previously exited 0, so a caller
  or CI step reading the exit code saw success over a partial failure.

  **This does not close the exit-code-honesty class, and should not be read as doing so.**
  The class is tracked in `grip#886`, which records 9+ known instances; two are fixed here.
  **`gr push` — the most consequential instance, the one that can silently lose work — is
  untouched by this release.**
- **`gr link --apply` reports a stale source instead of composing it silently** and
  **gripspace pins are checked for freshness**.

  **Two measured holes in this remain open** (`grip#891`), and this feature ships with **no
  user-facing documentation** — `README.md` has no `gr link` section, only a one-line table
  mention. Link freshness is **not** guaranteed by this release; what changed is that one class
  of stale composition now reports rather than proceeding quietly.

### Known gaps at this release
- The new exit-code semantics (`EXIT_REFUSED=2`, operational failure `1`, success `0`) are not
  documented in `README.md` for either `gr checkout` or `gr pr merge`.
- `CONTRIBUTING.md` does not mention gr2 anywhere, despite **9 of the 14 first-parent units in
  `v1.1.0..6192e8c5` touching only `gr2/`** — the same first-parent classification used
  throughout this entry. A contributor following that document has no path to discovering gr2
  exists or how to set up its separate Python environment.

  **Two earlier drafts of this line carried a commit-level statistic; both are withdrawn, and
  the second is the more instructive.** The first said gr2 was "roughly a third of this
  repository's active commit volume" — no range, no metric, nothing to reproduce. The second
  replaced it with "22 of 42 commits (52%), measured by first-parent diff," which named a range
  and a method and was **still wrong, because the number did not come from the method it
  named**: 22 is `git rev-list --count -- gr2/`, whose path-history simplification silently drops
  eight merge commits that *do* touch `gr2/` against their first parent. Under the stated method
  the figure is 30 of 42 (71%). A sourced number can be more misleading than an unsourced one,
  because citing a method invites trust the number has not earned. The unit measure above needs
  no footnote, so it is the only one kept.

## [1.1.0] - 2026-08-13

These entries sat under an `[Unreleased]` heading until 2026-08-21. **That heading was false:**
this work shipped in `v1.1.0`, tagged 2026-08-13 and published to crates.io. Verified by
checking the feature's own symbols into the `v1.1.0` tree rather than by commit archaeology,
with a negative control. A reader trusting the old heading would have believed a shipped
feature was still pending.

### Fixed
- **`gr pr merge` no longer defaults to squash** (#829) — with no `--method`, the command queried the host for its allowed methods and took the first of squash > merge > rebase, so on any repo permitting squash the tool actively chose it. A workspace whose policy is merge-commit-only got squashes from its own tooling, and on a private repo — where hosting rulesets are unavailable on most plans — nothing downstream could reject the result. The default is now a real merge commit, configurable via `settings.merge_method` in the manifest. Choosing is the workspace's job; the host is asked only whether the choice is permitted.
- **An unavailable merge method is refused, not substituted** (#829) — if the chosen method is not permitted by the host, `gr pr merge` says so and stops rather than quietly using a different one. Silent substitution is the original defect in a politer form: the operator asks for one strategy, another happens, and the only way to find out is to count parents afterwards.
- **`gr pr merge` verifies the merge commit has two parents** (#829) — after a `merge`, the resulting commit is checked against the LOCAL repository rather than the platform's response, because the API reports that a merge happened and not which strategy produced it. Squash and rebase are exempt: they produce single-parent commits by design, and warning on them would make the check noise.

### Changed
- **`gr pr merge` gates are individually waivable, and what is waived is printed at the moment of the merge** (#837) — `--force` was a single boolean that suppressed the approval, checks and mergeability gates together. In a workspace whose ratification convention is review *comments* rather than formal platform approvals, the approval gate can never pass, so `--force` became mandatory on every merge — and took the other gates with it, silently. A gate that must always be bypassed does not gate; it trains the bypass. New `--skip-gate <approval|checks|mergeable>` waives exactly one, repeatable; `--force` still waives all but now names what it suppressed. An unknown gate name is an error rather than a silent no-op, because a misspelled waiver that quietly waives nothing reads exactly like a gate that passed.
- **`gr pr merge` prints the resolved target and method at the moment of the irreversible act** (#837) — `owner/repo#N branch -> base method=Merge`, printed durably immediately before the merge call rather than from the plan beforehand or the result afterwards. `gr pr merge` takes no PR number: it merges whatever PR the current branch owns, so the operator's belief about the target and the command's resolution of it are separate facts. Everything that goes wrong with this command goes wrong in that gap.

## [1.0.1] - 2026-07-23

### Fixed
- **`gr pr edit`/`review`/`merge` refuse unscoped multi-repo actions** (#773) — when a caller doesn't pass `--repo` and more than one repo has a matching open PR for its checked-out branch, gr now fails closed and lists exactly what would have been touched instead of silently fanning out. A repo's checked-out branch can be a stale, forgotten one from unrelated past work, so "N repos matched" was never the same thing as "the user meant N repos" — this closed two real incidents (grip#770, grip#771) where a body meant for one PR landed on someone else's unrelated one.
- **`gr checkout add` produces a self-discoverable child workspace** (#775) — unified the workspace discovery walk so a child checkout's manifest is found the same way regardless of nesting, removed a checkout/manifest-repo collision, and made malformed or reconstructed checkout metadata fail closed rather than silently carrying authority it shouldn't have.
- **`gr pr merge --wait` detects no-checks-configured and returns immediately** (#776) — previously could hang waiting on checks that were never going to run; now closes the false-absence and mutation-silence gaps in the re-poll guard.

## [1.0.0] - 2026-04-17

gr1 reaches stable. This release marks the feature-complete gr1 CLI before gr2 takes over as the primary workspace management system.

### Summary

gr1 ships as a production-ready multi-repo workspace tool with:

- **Manifest-based configuration**: declarative YAML workspace definitions with composable gripspace inheritance, named remotes, and fork workflow support
- **Synchronized operations**: branch, checkout, add, commit, push, pull, rebase, sync, and diff across all repos in parallel
- **Linked PR workflow**: create, review, merge, and track pull requests that span multiple repos with all-or-nothing merge strategy and auto-merge support
- **Griptrees**: worktree-based parallel workspaces for simultaneous multi-branch development with per-repo upstream tracking
- **Multi-platform support**: GitHub, GitLab, Azure DevOps, and Bitbucket with platform-specific adapters and rate limiting
- **Agent orchestration**: `gr spawn` for multi-agent session management with tmux, graceful shutdown, and configurable restart policies
- **Developer tooling**: grep, prune, gc, cherry-pick, verify, release, benchmarks, shell completions, MCP server, and agent context generation
- **Security hardening**: path traversal protection, credential sanitization, mutex poison recovery, thread panic propagation

27 releases. 60+ commands. 4 hosting platforms. Production-tested across the synapt multi-agent team since January 2026.

### Deprecation Notice

**gr1 is now in maintenance mode.** gr2 (Python-first workspace orchestration) is the active development target. gr2 introduces declarative workspace specs, execution plans, lane-aware commands, and a migration path from gr1 gripspaces.

See `gr2/docs/GR2-MVP.md` for the gr2 feature set and `gr2/docs/GR1-GR2-MIGRATION-PLAYBOOK.md` for migration instructions.

New features will land in gr2. gr1 will receive only critical bug fixes.

## [0.20.0] - 2026-04-14

### Added
- **`gr spawn down` graceful shutdown** (#567)
  - Three-phase shutdown: send `/exit` to agents, poll `pane_dead`, force-kill remaining
  - `pane_exit_state()` with tri-state return (`Option<bool>`) for proper tmux error handling
  - Per-agent shutdown via `--agent <name>` flag
  - Configurable timeout via `--timeout` flag (default 10s)
  - Full error reporting on all tmux operations

- **Python-first gr2 workspace orchestration** (#566)
  - Complete Python gr2 CLI with typer: spec, plan, apply, exec, review, workspace, repo, lane, lease commands
  - Cache-backed materialization for workspace apply
  - Structured hook runtime with dataclasses and lifecycle stages
  - Review checkout-pr with remote refetch on existing branches
  - gr1 detect and migration commands

## [0.19.0] - 2026-04-14

### Added
- **gr2 team-workspace model** — declarative spec, plan, and apply lifecycle
  - `gr2 init` creates team workspace structure (agents/, repos/, .grip/)
  - `gr2 spec show/validate` for workspace spec management
  - `gr2 plan` diffs workspace spec into an execution plan
  - `gr2 apply` materializes repos via git clone into unit workspaces (#514)
  - `gr2 apply --autostash` automatically stashes and restores dirty repos (#534)
  - Partial unit convergence: detects and clones missing repos in existing units (#539)
  - `gr2 team add/list/remove` for agent workspace management
  - `gr2 repo add/list/remove` for repo registry
  - `gr2 unit add/list/remove` for unit registry
  - Guard checks with dirty state detection via `git status --porcelain`
  - Stash state audit trail in `.grip/state/stash.toml`
- **Checkout lifecycle commands** (#489) — `gr checkout --create`, `--orphan`
- **Cache-backed checkout creation** (#485) — checkout creates from local cache when available
- **Machine-level manifest repo caches** (#484) — shared manifest caches across workspaces
- **`gr migrate in-place`** (#458) — upgrade existing workspaces without re-cloning
- **E2E demo script** (#454) — automated release verification

### Changed
- **gr2 binary removed from main crate** — gr2 development continues as a standalone Python CLI; Rust gr2 code retained as library
- CI: Windows tests run in non-blocking lane (#487)
- Stripped non-OSS prompts from migrate flow (#510)

### Fixed
- Spawn model passthrough from agents.toml (#474)
- Root manifest creation during migrate in-place (#467)
- Auto-reclone spaces/main when not a git repo (#470)
- Migrate linked worktrees into griptrees (#466)
- Preserve .env at gripspace root during worktree repair
- Stable pane IDs for dashboard targeting (#453)
- Benchmark CI fixture literals

## [0.17.1] - 2026-03-12

### Added
- **`gr issue` commands** (#239)
  - `gr issue list` — list issues with state, label, and assignee filters
  - `gr issue create -t "title"` — create issues with body, labels, and assignees
  - `gr issue view <number>` — view full issue details
  - `gr issue close <number>` — close an issue (with PR guard)
  - `gr issue reopen <number>` — reopen an issue (with PR guard)
  - Per-repo targeting with `--repo` flag; auto-selects single-repo workspaces
  - `--json` output for all subcommands
  - MCP server integration via `gitgrip_issue` tool
  - Pagination to fill requested `--limit` despite GitHub's mixed issues/PRs endpoint
  - URL-encoded query parameters for labels and assignees
- **`gr pr edit` command** (#257)
  - Update PR title and/or body across linked PRs
  - `gr pr edit --title "new title"` to update title
  - `gr pr edit --body "new body"` to update body
  - Updates all open PRs on the current branch across repos
  - JSON output support with `--json`
- **`gr pr create` multi-branch support** (#225)
  - Repos on different branches are now grouped by branch with PRs created per group
  - `--repo` filter for targeting specific repos
- **`gr push` skip repos with no unique commits** (#372)
  - New branches with no unique commits are skipped instead of causing push errors

### Fixed
- **`gr add` partial staging** (#374)
  - Stage files individually so one missing path doesn't block the rest
  - Warn instead of error when a pathspec doesn't match in a repo

## [0.17.0] - 2026-03-11

### Added
- **Init wizard with auto-detection** (#361)
  - `gr init --from-dirs` now auto-detects language, package manager, and toolchain commands for 9 languages (Rust, TypeScript, JavaScript, Python, Go, Ruby, Java, C++, C)
  - Interactive multi-step wizard: repo selection, post-sync hooks, agent context targets, manifest review
  - Auto-populates per-repo `agent:` config (language, build, test, lint, format commands)
  - Generates workspace scripts (`build-all`, `test-all`, per-repo variants)
  - Generates `workspace.hooks.post_sync` for repos with install commands (e.g., `pnpm install`, `uv sync`)
  - Generates `workspace.agent` with description, conventions, and workflows
  - TTY auto-detection: wizard runs automatically on TTY, non-interactive when piped
  - `--no-interactive` flag to explicitly skip the wizard
  - YAML section comments in generated manifests
- **`gr restore` command** (#359)
  - Unstage files with `gr restore --staged`
  - Discard working tree changes with `gr restore`
  - `--repo` filtering support
- **Per-repo `--repo` filtering on all commands** (#358)
- **`gr target` command** for managing PR base branches (#336)
- **MCP stdio server** for agent tool access (#335)
  - CLI passthrough tools and resource endpoints
  - Cancellation support and bounded output capture
- **`gr pr list` and `gr pr view` commands** (#202)
  - `gr pr list` — list PRs across all repos with `--state`, `--repo`, `--limit` filters
  - `gr pr view [number]` — view PR details, reviews, and body (auto-detects from current branch)
  - `list_pull_requests` platform trait method with GitHub implementation

- **`gr pr checks` improvements** (#238)
  - `--repo` flag to filter checks to a specific repository
  - Skip reference repos from checks output
  - Deduplicate stale check runs (prefer terminal states over pending for same context)

### Fixed
- Deep-merge gripspace repo config with local overrides (#356)
- Status typechanges, in-progress operations, and tree list detection (#355)
- Azure PR diff auth and `--no-delete-branch` flag (#345)
- Surface per-repo errors in `gr pr create/merge` summaries (#344)
- Manifest repo handling in branch and sync (#357)

### Changed
- Extract command dispatch from `main.rs` into `src/cli/dispatch.rs` (#352)
- Introduce option structs for complex function signatures (#350)
- Change `&PathBuf` to `&Path` across all function signatures (#349)
- Extract shared HTTP client creation for platform adapters (#348)
- Remove duplicated code and unused test helpers (#347)
- Wire up rate limiting across platform adapters (#351)

## [0.16.0] - 2026-02-20

### Breaking Changes
- **Manifest v2 schema** (#332)
  - `default_branch` renamed to `revision` (aligns with git-repo terminology)
  - `target` is now a branch name only (e.g. `develop`), not `remote/branch` format
  - New `sync_remote` field: remote for fetch/rebase (default: `origin`)
  - New `push_remote` field: remote for push (default: `origin`)
  - New top-level `remotes` section for named remotes with base fetch URLs
  - Repos can use `remote: upstream` instead of explicit `url` (URL derived from remote base + repo name + `.git`)
  - Default version bumped to 2
  - v1 manifests auto-migrate: `default_branch` aliased to `revision`, `target: upstream/develop` split into `target: develop` + `sync_remote: upstream`

### Added
- **Named remotes** — declare remotes at manifest top level with base URLs for URL derivation
- **`ensure_remote_configured`** — `gr sync` auto-configures declared remotes (e.g. upstream) in repos
- **Fork workflow support** — separate `sync_remote` (fetch from upstream) and `push_remote` (push to fork)

## [0.15.0] - 2026-02-18

### Added
- **Per-repo workflow `target` branch** (#329, #330)
  - New `target` field on repos and settings for specifying the workflow target branch (PRs, pull, rebase, prune, sync)
  - Supports `remote/branch` format (e.g. `origin/develop`, `upstream/main`) for fork workflows
  - Resolution chain: `repo.target` → `settings.target` → `repo.default_branch` → `settings.default_branch` → `"main"`
  - `default_branch` is now optional at repo level, inheritable from `settings.default_branch`
  - `gr repo add --target` flag for setting target on new repos
  - `gr status` column renamed from "vs main" to "vs target"
  - Full backward compatibility: existing manifests without `target` behave identically

## [0.14.1] - 2026-02-13

### Fixed
- **`gr pr merge --method squash`** no longer switches repos to a branch named "squash" (#251)
  - Merge method now uses clap `ValueEnum` for type-safe parsing
  - Invalid values are rejected at CLI level with `[possible values: merge, squash, rebase]`
  - Removed ambiguous `-m` short flag (use `--method` instead)
- **`gr init --from-dirs`** now detects the remote's default branch instead of using the current local branch (#310)
  - Checks `origin/HEAD` symref, then remote tracking branches, then local branches
  - Previously picked up feature branches as `default_branch` in the generated manifest

### Changed
- Improved test coverage from 57.84% to 59.56% with 90+ new unit tests across 14 files (#325)

## [0.14.0] - 2026-02-13

### Added
- **`gr sync --rollback`** - Rollback all repos to their state before the last sync (#323)
  - Pre-sync snapshot saved to `.gitgrip/sync-state.json`
  - Restores each repo's branch and HEAD commit
- **GitBackend/GitRepo traits** - Testable git abstraction layer for swappable implementations (#322)
- **OutputSink trait** - Testable output abstraction with `TerminalSink` and `BufferSink` (#321)
- **Platform capability matrix** - Documentation and code showing which operations each platform supports (#320)
- **GitHub Pages landing page** (#307)

### Changed
- **WorkspaceContext** consolidates workspace_root, manifest, quiet, verbose, json, sink, and git_backend into a single struct passed to commands (#318)
- **Trimmed tokio features** from `"full"` to `["rt-multi-thread", "macros", "process", "time"]` reducing compile-time dependencies (#319)
- Moved repo-local skill files to gripspace for single-source management (#311)

### Fixed
- **Thread panic handling** - JoinSet tasks now propagate panics instead of silently dropping them (#315)
- **Mutex poison recovery** - Git status cache recovers from poisoned locks instead of panicking (#315)
- **Branch `--json` output** - JSON mode now works correctly for branch operations (#316)
- **Git lock detection** - Detects stale `.git/index.lock` files and provides actionable guidance (#316)
- **GHE auto-merge** - GitHub Enterprise auto-merge now works correctly (#316)
- **Windows stack overflow** on debug builds resolved (#314)
- **GitHub platform test failures** resolved (#312)

## [0.13.0] - 2026-02-11

### Added
- **`gr agent` command** - AI coding tool context discovery and workspace operations (#289)
  - `gr agent context` — Full workspace context with repos, build commands, and conventions
  - `gr agent context --repo <name>` — Single repo context
  - `gr agent build <repo>` / `gr agent test <repo>` — Run build/test for a specific repo
  - All subcommands support `--json` for machine consumption
- **`gr agent generate-context`** - Multi-tool context generation from a single source (#290)
  - Define context once in manifest, generate for Claude, OpenCode, Codex, Cursor, and raw formats
  - `{repo}` placeholder in dest paths generates per-repo skill files
  - `compose_with` appends additional files to generated context
  - Runs automatically during `gr sync`; standalone with `gr agent generate-context`
  - `--dry-run` flag to preview without writing
- **`gr verify`** - Boolean pass/fail assertions for CI and scripting (#284)
  - `--clean` — Assert all repos have no uncommitted changes
  - `--on-branch <name>` — Assert all repos are on a specific branch
  - `--synced` — Assert repos are up to date with remote
  - Returns exit code 0/1 for scripting; supports `--json` output
- **`gr release`** - Automated release workflow (#287)
  - Bumps version, updates changelog, creates PR, tags, and creates GitHub release
  - `--dry-run` for preview
- **`--json` global flag** - Machine-readable JSON output on all commands (#282)
  - Structured output for `gr status`, `gr diff`, `gr pr status`, `gr verify`, `gr link --status`, and more
- **`--wait` flag for `gr pr merge`** - Poll CI checks before merging (#285)
  - Configurable timeout with `--timeout <seconds>` (default: 300s)
  - Visual spinner showing elapsed time and check status
- **Post-sync hooks** - Run commands automatically after `gr sync` (#286)
  - `post_sync` hooks in workspace manifest with optional `condition` triggers
  - `file_changed` condition to only run when specific files change
- **Agent manifest config** - Define agent-relevant metadata per-repo in manifest (#288)
  - `agent.description`, `agent.language`, `agent.build`, `agent.test`, `agent.lint`
  - Workspace-level `agent.conventions` for cross-repo coding standards

### Fixed
- **`gr pr merge` silent failure** - Now verifies PR state after merge API call to catch cases where GitHub reports success but PR wasn't actually merged (#283)
- **`gr pr merge --wait` timeout** - Timeout now properly bails with error instead of silently allowing merge to proceed (#305)
- **`gr pr merge --wait` early exit** - Checks loop now exits early when all checks have definitively resolved (no more pending)
- **Gripspace repo groups** - Repos inherited from included gripspaces can now be added to groups (#294)

## [0.12.3] - 2026-02-10

### Added
- **Auto-apply links on sync** - `gr sync` now automatically applies linkfiles and copyfiles after syncing repos, eliminating the need to manually run `gr link --apply` (#279)

## [0.12.2] - 2026-02-10

### Added
- **ASCII logo** - Running `gr` with no subcommand now displays a colored ASCII art logo and wordmark (#244)

## [0.12.1] - 2026-02-10

### Fixed
- **Gripspace-inherited repos now visible to all commands** - `gr status`, `gr repo list`, `gr branch`, and all other commands now see repos inherited from gripspace includes, not just `gr sync` (#273)

### Changed
- **Renamed internal manifest loader** - `load_workspace()` → `load_gripspace()`, `resolve_workspace_manifest_path()` → `resolve_gripspace_manifest_path()` for naming consistency with gripspace terminology

## [0.12.0] - 2026-02-09

### Added
- **Gripspace includes** - Composable manifest inheritance via `gripspaces:` directive (#270)
  - Clone external gripspace repositories and merge their repos, scripts, env, hooks, linkfiles, and copyfiles into the local workspace
  - Recursive resolution with DAG-aware cycle detection (max depth 5)
  - Local manifest values always win on conflict
  - `gr sync` and `gr init` resolve and clone gripspaces automatically
  - `gr status` shows gripspace clone status with revision and dirty state
- **Composefile support** - Generate files by concatenating parts from gripspaces and/or local manifest (`composefile:` directive)
  - Processed on `gr sync` and `gr link --apply`
  - Parts can reference gripspace content or local manifest content
- **URL normalization** - SSH and HTTPS URLs to the same repo are recognized as equivalent for space reuse (`git@github.com:user/repo` ↔ `https://github.com/user/repo`)
- **Manifest paths module** - Consistent path resolution across all commands (`manifest_paths.rs`)

### Changed
- **Unified directory layout** - `.gitgrip/spaces/` is now the single directory for all space content (gripspaces and manifest), replacing the previous split between `gripspaces/` and `spaces/`
- Reserved space names (`main`, `local`) are auto-suffixed to avoid conflicts with the manifest space

### Security
- Gripspace name validation with allowlist (`[a-zA-Z0-9._-]`), rejecting `.`, `..`, and path traversal
- Windows absolute path and UNC path rejection in path boundary checks
- Gripspace manifest validation via `validate_as_gripspace()` (allows empty repos, validates all other constraints)
- Failed clone cleanup — partial directories are removed on clone error
- Untrusted gripspace path hardening across all validators

## [0.11.3] - 2026-02-09

### Changed
- **Griptree branch tracking at creation** - `gr tree add` now sets each repo's griptree branch to track its upstream default (`origin/main`, `origin/dev`, etc.) (#267)
- **Griptree sync self-healing** - `gr sync` now repairs branch upstream tracking when on the griptree base branch, using per-repo mapping from `griptree.json` (#267)

### Documentation
- Updated workflow docs to prefer `gr checkout --base` after merge cleanup
- Updated command docs to include `gr tree return` and `gr sync --reset-refs`

### Testing
- Added integration coverage for upstream tracking during `gr tree add`
- Added integration coverage for upstream tracking repair during `gr sync`

## [0.11.2] - 2026-02-09

### Fixed
- **Griptree registry resolution** - `gr tree list` now resolves griptrees from the main workspace when run inside a griptree (#263)
  - Also applies to `gr tree remove`, `gr tree lock`, and `gr tree unlock`
  - Added regression coverage for list/remove/lock from griptree workspaces
- **Worktree-safe ref reset** - `gr sync --reset-refs` now falls back to detached checkout when target branch is locked by another worktree (#265)
  - Prevents false failures when refs are shared across multiple worktrees
  - Keeps hard reset behavior and adds explicit fallback status output

## [0.11.1] - 2026-02-08

### Fixed
- **Reference repo sync alignment** - `gr sync --reset-refs` now checks out the correct upstream branch before hard-resetting (#259)
  - Warns before discarding uncommitted changes or unpushed commits
  - Properly aligns reference repos to upstream branch (e.g., `origin/dev`) instead of staying on wrong branch
  - New `checkout_branch_at_upstream` git helper with worktree conflict detection

### Added
- `gr tree return` command to switch back to main workspace (#254)
- `gr sync --reset-refs` flag to hard-reset reference repos to upstream (#255)

## [0.11.0] - 2026-02-07

### Added
- **Griptree upstream tracking** - Per-repo upstream branch configuration for griptrees (#246)
  - `gr tree add` now auto-detects and records upstream for each repo in `griptree.json`
  - `gr sync` uses per-repo upstream mapping when on griptree base branch
  - `gr rebase --upstream` uses griptree upstream mapping instead of hardcoded `origin/main`
  - `gr checkout --base` - new flag to checkout the griptree base branch across all repos
  - Upstream validation with clear error messages for malformed refs
- **Pull command** - `gr pull` for pulling changes across repos (#234)
  - `--rebase` flag for rebase-based pulls
  - `--sequential` flag for ordered output
  - `--group` flag for group-scoped pulls
- **Terminal UI improvements** - verbose mode and debug output (#232)
- **Rate limiting** - Infrastructure for platform API rate limiting (#153)
- **`--verbose` global flag** - Shows external commands being executed (#204)

### Changed
- `gr rebase --upstream` now uses griptree config for per-repo upstream resolution
- Refactored `InitOptions` and `BranchOptions` into dedicated structs (#222)
- Simplified `check_repo_for_changes` in PR merge (#221)
- Consolidated duplicate manifest helper functions (#220)

### Fixed
- **Production readiness hardening** (#247)
  - Replaced `process::exit(1)` with proper error propagation in commit command
  - Poisoned mutex recovery in git status cache (4 locations)
  - Safe `SystemTime` fallback in retry jitter
  - Descriptive `expect()` for hardcoded progress bar templates
  - Improved path traversal detection with depth-tracking segment walk
  - Credential sanitization in git command logging
  - Symlink destination validation within workspace boundaries
  - HTTP client fallback logging in GitLab, Azure DevOps, and Bitbucket adapters
- Fixed HTTP client recursion in platform adapters (#242)

### Testing
- 75+ new integration tests covering edge cases and error scenarios
- Pull error handling tests (#235)
- Unit tests for rate_limit, types, bench, and PR commands (#228)
- PR merge command integration tests (#206)

## [0.10.0] - 2026-02-05

### Added
- **Parallel sync** - `gr sync` now runs in parallel by default for faster syncing
  - Use `--sequential` flag for sequential sync (previous behavior)
- **Checkout create flag** - `gr checkout -b <branch>` creates and switches to branch in one command
  - Creates branch if it doesn't exist, checks out if it does
- **Manifest schema command** - `gr manifest schema` displays manifest specification
  - `--format yaml` (default), `--format json`, or `--format markdown`
- **Group management** - Interactive repo grouping commands
  - `gr group add <group> <repos...>` - add repos to a group
  - `gr group remove <group> <repos...>` - remove repos from a group
  - `gr group create <name>` - shows how to create groups

### Changed
- **Consistent manifest handling** - Manifest repo now included in all operations:
  - `gr sync` - syncs manifest repo first
  - `gr branch` - creates/deletes branches in manifest repo
  - `gr checkout` - checks out manifest repo with other repos
  - `gr push` - pushes manifest repo if it has changes
  - `gr diff` - shows manifest repo changes
- Added `get_manifest_repo_info()` helper in `src/core/repo.rs` for reusable manifest repo handling

### Fixed
- Manifest repo was inconsistently handled across commands (fixes #210, #214)

## [0.9.0] - 2026-02-05

### Added
- `gr pr merge --update` flag - automatically update branch from base when behind, then retry merge
- `gr pr merge --auto` flag - enable auto-merge so PRs merge when all required checks pass
- `BranchBehind` error variant - detects when PR branch is behind base and provides actionable hint
- `BranchProtected` error variant - detects branch protection rule violations and suggests `--auto` or `--admin`
- `update_branch` platform trait method with GitHub implementation (PUT update-branch API)
- `enable_auto_merge` platform trait method with GitHub implementation (via `gh` CLI)
- wiremock tests for branch-behind, branch-protected, update-branch success, and update-branch conflict scenarios

### Changed
- `merge_pull_request` in GitHub adapter now uses raw HTTP instead of octocrab for proper error classification
  - octocrab swallowed HTTP response bodies, making it impossible to distinguish error types
  - Now correctly parses 405 (branch behind), 403 (branch protected), and other failure modes
- CI workflow no longer uses path filters - runs on every push/PR to main (#175)

### Fixed
- `gr pr merge` "not mergeable" message now suggests `--update` when branch may be behind base

## [0.8.0] - 2026-02-04

### Added
- `gr prune` command - delete local branches merged into the default branch
  - Dry-run by default, `--execute` to actually delete
  - `--remote` flag to also prune remote tracking refs (`git fetch --prune`)
  - Reports summary of pruned branches across repos
- `gr grep` command - cross-repo search using `git grep`
  - Prefixes results with repo name for easy identification
  - `-i` flag for case-insensitive search
  - `--parallel` flag for concurrent search across repos
  - Supports pathspec filtering (`gr grep "pattern" -- "*.rs"`)
- Test harness with 40+ integration tests (Phases 0-3)
  - `WorkspaceBuilder` fixture for creating temporary workspaces with bare remotes
  - git_helpers module for test git operations
  - wiremock-based platform mocks for GitHub/GitLab/Azure
  - Tests for branch, checkout, sync, add, commit, push, status, forall, griptree, PR, and error scenarios

### Fixed
- `gr pr create` now includes repos without remote tracking branches
  - Previously `has_commits_ahead()` returned false when base ref was missing, silently skipping repos
  - Now assumes the branch has changes when neither remote nor local base ref exists
- Griptree worktree name parsing bug fix for names with path separators

### Improved
- Error messages across 5 key files with actionable recovery suggestions:
  - Push errors: interpreted messages for non-fast-forward, auth failure, network issues
  - PR create: clearer branch reference errors with guidance
  - Init: recovery suggestions for existing directory and missing manifest
  - Run: suggests `gr run --list` for missing scripts
  - Push: suggests `gr sync` for missing remote targets

## [0.7.1] - 2026-02-03

### Fixed
- `gr pr merge --force` now properly bypasses `all-or-nothing` merge strategy (#180)
  - Previously would stop on first failed merge even with `--force` flag
  - Now continues merging remaining PRs when one fails with `--force`
  - Shows warning for failed merges instead of hard stop
- `gr pr create` now detects uncommitted changes in manifest repo (#178)
  - Previously only checked for commits ahead of default branch
  - Now detects staged and unstaged changes as well
  - Properly handles manifest-only PR creation

### Documentation
- Updated skill documentation with complete manifest schema
- Added workflow patterns section (accidental main branch commits, single-repo operations)
- Documented `reference` repos and `platform` configuration options
- Added IMPROVEMENTS.md entries for discovered friction points

## [0.7.0] - 2026-02-02

### Changed (Breaking)
- `gr forall` now defaults to running commands only in repos with changes
  - Use `--all` flag for previous behavior (run in all repos)

### Added
- `gr branch --move` flag to move commits from current branch to a new branch
  - Creates new branch at HEAD, resets current branch to remote, checkouts new branch
- `gr branch --repo <names>` flag to operate on specific repos only

### Fixed
- Platform API timeouts now have explicit configuration (10s connect, 30s read/write)
  - Faster failure detection and clearer error messages
- Worktree branch conflicts now show helpful error messages with guidance
  - Explains the git limitation and suggests alternatives

## [0.6.0] - 2026-02-02

### Fixed
- Griptree branches now base off repo's default branch instead of HEAD
- Griptree worktrees now use griptree branch name, not current workspace branch
- Reference repo sync failures no longer block griptree creation (warning only)
- Automatic link application after griptree creation
- Manifest repo links (copyfile/linkfile) now properly processed
- Worktree cleanup on griptree removal using git2 prune
- Rollback on partial griptree creation failure
- Clone fallback when specified branch doesn't exist on remote

### Added
- Legacy griptree discovery in `gr tree list`
- Worktree tracking metadata (worktree_name, worktree_path, main_repo_path)
- state.json initialization in new griptrees

## [0.5.8] - 2026-02-02

### Added
- `gr pr create` now supports `-b/--body` flag for non-interactive PR description
- Griptree manifest worktree support - each griptree can have its own manifest worktree
- Branch tracking for griptrees - tracks original branch per repo for proper merge-back
- Reference repo sync - reference repos auto-sync with upstream before worktree creation
- `gr add` and `gr commit` now handle manifest worktree changes automatically
- `gr status` displays manifest worktree status as separate entry
- Griptree worktrees now prioritize repo's current branch instead of griptree branch
- Comprehensive test coverage for manifest worktree functionality (10 new tests)
- Documentation in IMPROVEMENTS.md for tracking completed and pending features
- Worktree conflict troubleshooting guide added to CONTRIBUTING.md
- Documentation for IMPROVEMENTS.md merge conflict behavior
- PLAN document for griptree repo branch implementation

### Changed
- Manifest loading prioritizes griptree's own manifest, falls back to main workspace
- IMPROVEMENTS.md reorganized to show completed vs pending features clearly

## [0.5.7] - 2026-02-01

## [0.5.6] - 2026-02-01

### Added
- Full Bitbucket API integration with PR create/merge/status/comment support
- `gr pr create` supports `--dry-run` for preview without creating actual PRs
- `gr pr create` supports `--push` flag to push branches before creating PRs
- Shell completions for bash, zsh, fish, elvish, powershell via `gr completions <shell>`

### Changed
- `gr sync` now succeeds when on a branch without upstream configured
- `gr push` now shows which repos failed and why
- Better CI status visibility in PR checks output
- Improved sync error messages showing which repos failed

### Fixed
- PR merge now recognizes passing GitHub Actions check runs correctly
- `gr repo add` YAML insertion correctly places repos under `repos:` section
- Griptree creation now writes `.griptree` pointer file for workspace detection
- Windows CI: Fixed libgit2-sys linking by adding advapi32.lib

## [0.5.5] - 2026-02-01

### Added
- Telemetry, tracing, and benchmarks infrastructure for performance monitoring
  - Optional telemetry feature flag
  - Correlation IDs for request tracing
  - Git operation metrics (fetch, pull, push timing)
  - Platform API metrics
- CI now triggers for markdown file changes (enables doc-only PRs)

### Fixed
- `gr sync` now succeeds when on a branch without upstream configured
  - Fetches from origin to update refs instead of failing
  - Reports "fetched (no upstream)" status
- Windows CI: Fixed libgit2-sys linking by adding advapi32.lib via build.rs and RUSTFLAGS
- `gr repo add` YAML insertion now correctly places repos under `repos:` section
- Griptree creation now writes `.griptree` pointer file for workspace detection

### Changed
- CI summary job added for branch protection compatibility

## [0.5.4] - 2026-02-01

### Added
- Reference repos feature - mark repos as read-only with `reference: true` in manifest
  - Reference repos are excluded from `gr branch`, `gr checkout`, `gr push`, and PR operations
  - Reference repos still sync with `gr sync` and appear in `gr status` with `[ref]` indicator
  - Useful for upstream dependencies, reference implementations, or docs you only read
- `gr status` now shows `[ref]` suffix for reference repos

## [0.5.3] - 2026-01-31

### Added
- `gr status` now shows "vs main" column with commits ahead/behind default branch
  - `↑N` for commits ahead of main
  - `↓N` for commits behind main
  - `-` when on the default branch
  - `✓` when feature branch is in sync with main
- Summary line shows count of repos ahead of main

## [0.5.2] - 2026-01-31

### Fixed
- Git operations now work correctly in griptree worktrees
  - Changed all git CLI calls to use `repo.workdir()` instead of `repo.path().parent()`
  - Fixes "fatal: this operation must be run in a work tree" errors for `gr sync`, `gr add`, `gr commit`, etc.
- Release workflow now uses `--allow-dirty` for crates.io publish to handle Cargo.lock changes

### Added
- Shell completions via `gr completions <shell>` (bash, zsh, fish, elvish, powershell)
- GitLab E2E PR workflow tests with Bearer token authentication
- `get_workdir()` helper function for worktree-compatible path resolution

## [0.5.1] - 2026-01-31

### Fixed
- `gr` commands now work from griptree directories by detecting `.griptree` marker file
  - Reads `mainWorkspace` field from `.griptree` and delegates to parent workspace
  - Fixes "fatal: this operation must be run in a work tree" errors when running from griptrees

## [0.5.0] - 2026-01-31

### Added
- `gr init --from-dirs` command to create workspace from existing local directories
  - Auto-scans current directory for git repositories
  - `--dirs` flag to scan specific directories only
  - `--interactive` flag for YAML preview and editing before save
  - Discovers remote URLs and default branches automatically
  - Handles duplicate names with auto-suffixing
  - Initializes manifest directory as git repo with initial commit

## [0.4.2] - 2026-01-29

### Fixed
- Griptree worktrees now use manifest paths (e.g., `./codi`) instead of repo names
- `gr` commands now work correctly from within griptree directories

## [0.4.1] - 2026-01-29

### Changed
- Renamed command from `gr griptree` to `gr tree` to avoid "gitgrip griptree" duplication
- Standalone references use "griptree" branding (e.g., "Create a griptree")
- Commands use `gr tree` (e.g., `gr tree add`, `gr tree list`)
- Config file remains `.griptree`

## [0.4.0] - 2026-01-29

### Added
- `gr tree` commands for worktree-based multi-branch workspaces (griptrees)
  - `gr tree add <branch>` - create parallel workspace on a branch
  - `gr tree list` - show all griptrees
  - `gr tree remove <branch>` - remove a griptree
  - `gr tree lock/unlock <branch>` - protect griptrees from removal
- `GitStatusCache` class for caching git status calls within command execution
- CI workflow with build/test/benchmarks on Node 18, 20, 22
- Griptree documentation graphics (`assets/griptree-concept.svg`, `assets/griptree-workflow.svg`)

### Changed
- **Performance:** Parallelized `push`, `sync`, and `commit` commands using `Promise.all()`
  - 3.4x speedup on `status` operation
  - 1.8x speedup on `branch-check` operation

## [0.3.1] - 2026-01-29

### Added
- `gr repo add <url>` command - add new repositories to workspace
  - Parses GitHub, GitLab, Azure DevOps URLs automatically
  - Updates manifest.yaml preserving comments
  - Clones repo and syncs to current workspace branch
  - Options: `--path`, `--name`, `--branch`, `--no-clone`

### Fixed
- `gr sync` no longer discards local commits on unpushed feature branches
  - Now checks if branch was ever pushed before auto-switching
  - Warns if local-only commits would be lost

## [0.2.4] - 2026-01-28

### Removed
- Removed backward compatibility for `.codi-repo/` directories
- Removed `gr migrate` command (no longer needed)

### Fixed
- Fixed PR linking - manifest PRs now include linked PR table with cross-references

## [0.2.3] - 2026-01-28

### Fixed
- Fixed PR linking - manifest PRs now include linked PR table with cross-references

## [0.2.2] - 2026-01-28

### Changed
- Updated branding with new emerald/green color scheme
- New icon design showing grip concept with central hub and three branches
- Updated README with centered banner and npm badges

## [0.2.1] - 2026-01-28

### Changed
- Renamed from `codi-repo` to `gitgrip`
- CLI command changed from `cr` to `gr`
- Directory changed from `.codi-repo/` to `.gitgrip/`
- Skill renamed from `codi-repo` to `gitgrip`
- All documentation updated to use new naming

### Added
- Backward compatibility for legacy `.codi-repo/` directories

## [0.2.0] - 2026-01-28

### Changed
- Initial release as `gitgrip` (renamed from codi-repo)

## [0.1.2] - 2026-01-27

### Added
- `gr forall` command - run commands in each repository (like AOSP repo forall)
- `gr add` command - stage changes across all repositories
- `gr diff` command - show diff across all repositories
- `gr commit` command - commit staged changes across all repositories
- `gr push` command - push current branch across all repositories
- `gr branch --repo` flag - create branches in specific repos only
- `--timing` flag for performance debugging
- `gr bench` command for benchmarking

### Fixed
- `gr pr create` now only checks branch consistency for repos with changes
- `gr pr status/merge` find PRs by checking each repo's own branch
- `gr sync` automatically recovers when manifest's upstream branch was deleted

## [0.1.1] - 2026-01-27

### Added
- Manifest repo (`.gitgrip/manifests/`) automatically included in commands
- `gr status` shows manifest in separate section
- `gr branch --include-manifest` flag

### Fixed
- Various stability improvements

## [0.1.0] - 2026-01-27

### Added
- Initial release
- `gr init` - initialize workspace from manifest
- `gr sync` - pull latest from all repos
- `gr status` - show status of all repos
- `gr branch` - create/list branches across repos
- `gr checkout` - checkout branch across repos
- `gr pr create` - create linked PRs
- `gr pr status` - show PR status
- `gr pr merge` - merge all linked PRs
- `gr link` - manage copyfile/linkfile entries
- `gr run` - execute workspace scripts
- `gr env` - show workspace environment variables
- Manifest-based configuration (AOSP-style)
- Linked PR workflow with all-or-nothing merge strategy
