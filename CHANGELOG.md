# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
