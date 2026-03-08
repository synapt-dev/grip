# gitgrip Development Guide

**git a grip** - Multi-repo workflow tool (Rust implementation)

## Build & Test

```bash
cargo build                # Build debug binary
cargo build --release      # Build release binary
cargo test                 # Run tests
cargo clippy               # Lint code
cargo fmt                  # Format code
cargo bench                # Run benchmarks
```

## Git Workflow

**IMPORTANT:** Never push directly to main. Never use raw `git` commands. Always use `gr` for all operations.

### Branch Strategy

**Main Branch (`main`)**
- Production-ready code
- Protected with PR requirements
- PRs target `main` by default (configurable via `gr target`)
- Use `gr sync` to stay current (not `git pull`)

**Feature Branches (`feat/*`, `fix/*`, `chore/*`)**
- All development happens here
- Short-lived, deleted after merge

### Standard Workflow

```bash
# 1. Start new work
gr sync                              # Pull latest from all repos
gr status                            # Verify clean state
gr target list                       # Check current PR target (should be main)
gr branch feat/my-feature            # Create branch across repos

# 2. Develop
gr diff                              # Review changes
gr add .                             # Stage changes across repos
gr commit -m "feat: description"     # Commit across repos
gr push -u                           # Push with upstream tracking

# 3. Create PR
gr pr create -t "feat: description" --push

# 4. Merge (after review + CI)
gr pr merge                          # Merge all PRs

# 5. Cleanup
gr sync                              # Pull latest and cleanup
gr checkout main                     # Switch back to main
```

### Epic Branch Workflow

For large features spanning multiple PRs, use `gr target` to redirect PRs to an epic branch:

```bash
# 1. Setup epic
gr sync && gr checkout main
gr branch epic/big-feature           # Create epic branch
gr push -u                           # Push epic branch

# 2. Set target to epic branch
gr target set epic/big-feature       # All PRs now target the epic

# 3. Work on sub-features (repeat as needed)
gr branch feat/sub-feature-1
# ... make changes, commit, push ...
gr pr create -t "feat: sub-feature-1" --push
gr pr merge
gr checkout epic/big-feature && gr sync

# 4. When epic is complete, reset target and merge epic → main
gr target set main                   # PRs target main again
gr checkout epic/big-feature
gr pr create -t "epic: big feature" --push
gr pr merge

# 5. Cleanup
gr sync && gr checkout main
```

### IMPORTANT: Never Use Raw Git

All git operations must go through `gr`. There is no exception.

```
❌ WRONG:
   git checkout -b feat/x
   git add . && git commit -m "msg" && git push
   gh pr create --title "msg"

✅ CORRECT:
   gr branch feat/x
   gr add . && gr commit -m "msg" && gr push -u
   gr pr create -t "msg" --push
```

`gr` manages all repos and the manifest together. Using raw `git` or `gh` bypasses multi-repo coordination and will miss the manifest repo.

### PR Review Process

**IMPORTANT: Never merge a PR without reviewing it first.** Always review your own PRs before merging.

**For AI agents (Claude, Codi, etc.):** Do NOT immediately merge after creating a PR. Always:
1. Create the PR with `gr pr create -t "title"`
2. Run `cargo build && cargo test` to verify nothing is broken
3. Check PR status with `gr pr status`
4. **Wait for GitHub checks to pass** - use `gh pr checks <number>` to verify
5. Review the diff with `gh pr diff <number>` (for each repo with changes)
6. Check feature completeness (see checklist below)
7. Only then merge with `gr pr merge` (if all checks pass and no issues found)

**CRITICAL: GitHub checks must pass before merging.** If checks are pending, wait. If checks fail, fix the issues first.

**Feature completeness checklist:**
- [ ] New command registered in `src/main.rs`
- [ ] Types added to appropriate module if needed
- [ ] Tests added for new functionality

**CRITICAL: Run benchmarks for performance-related changes:**

When modifying core git operations or command implementations:

```bash
# Run benchmarks
cargo bench

# Run workspace benchmarks (requires gitgrip workspace)
gr bench -n 10
```

Compare results before/after your changes. Document significant improvements or regressions in the PR description.

**CRITICAL: Update all documentation when changing commands/API:**
- [ ] `CLAUDE.md` - Development guide and command reference
- [ ] `README.md` - User-facing documentation
- [ ] `CONTRIBUTING.md` - If workflow changes
- [ ] `CHANGELOG.md` - Add entry for the change
- [ ] `codi-gripspace/skills/gitgrip/SKILL.md` - Skill definition (canonical source in gripspace)

Forgetting to update docs creates drift between code and documentation. Always check these files when adding/modifying commands.

### Release Process

When creating a new release:

1. **Update version numbers:**
   - [ ] `Cargo.toml` - version field
   - [ ] `CHANGELOG.md` - Add new version section with date

2. **Create and merge release PR:**
   ```bash
   gr branch release/vX.Y.Z
   # Update Cargo.toml version and CHANGELOG.md
   cargo build --release  # Updates Cargo.lock
   gr add . && gr commit -m "chore: release vX.Y.Z"
   gr push -u && gr pr create -t "chore: release vX.Y.Z"
   # Wait for CI, then merge
   gr pr merge --force
   gr checkout main && gr sync
   ```

3. **Create GitHub release:**
   ```bash
   gh release create vX.Y.Z --repo laynepenney/gitgrip --title "vX.Y.Z" --notes "Release notes..."
   ```
   This triggers GitHub Actions to automatically:
   - Build binaries for all platforms
   - Publish to crates.io
   - Create GitHub release with binaries

4. **CRITICAL: Update Homebrew formula (MUST DO EVERY RELEASE):**

   The `homebrew-tap` repo is part of the gitgrip workspace. Update it using `gr` commands:

   ```bash
   # Get SHA256 of the new release tarball
   curl -sL https://github.com/laynepenney/gitgrip/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256

   # Update the formula
   # Edit homebrew-tap/Formula/gitgrip.rb:
   #   - Update url to new version tag
   #   - Update sha256 to new hash

   # Commit and merge via gr
   gr branch chore/bump-gitgrip-X.Y.Z
   gr add . && gr commit -m "chore: bump gitgrip to vX.Y.Z"
   gr push -u && gr pr create -t "chore: bump gitgrip to vX.Y.Z"
   gr pr merge --force
   gr checkout main && gr sync
   ```

   **Forgetting to update Homebrew means users on `brew upgrade` won't get the new version.**

   Verify the update worked:
   ```bash
   brew update
   brew outdated  # Should show gitgrip if you have old version
   brew upgrade gitgrip
   gr --version   # Should show new version
   ```

## Project Structure

```
src/
├── main.rs               # CLI entry point (clap)
├── lib.rs                # Library exports
├── cli/                  # CLI command implementations
│   ├── mod.rs            # CLI module exports
│   ├── init.rs           # gr init
│   ├── sync.rs           # gr sync
│   ├── status.rs         # gr status
│   ├── branch.rs         # gr branch
│   ├── checkout.rs       # gr checkout
│   ├── add.rs            # gr add
│   ├── restore.rs        # gr restore
│   ├── diff.rs           # gr diff
│   ├── commit.rs         # gr commit
│   ├── push.rs           # gr push
│   ├── pull.rs           # gr pull
│   ├── rebase.rs         # gr rebase
│   ├── forall.rs         # gr forall
│   ├── grep.rs           # gr grep
│   ├── prune.rs          # gr prune
│   ├── gc.rs             # gr gc
│   ├── cherry_pick.rs    # gr cherry-pick
│   ├── ci.rs             # gr ci
│   ├── group.rs          # gr group
│   ├── manifest.rs       # gr manifest
│   ├── repo.rs           # gr repo
│   ├── link.rs           # gr link
│   ├── run.rs            # gr run
│   ├── env.rs            # gr env
│   ├── bench.rs          # gr bench
│   ├── tree.rs           # gr tree (griptrees)
│   └── pr/               # PR subcommands
│       ├── mod.rs
│       ├── create.rs
│       ├── status.rs
│       ├── merge.rs
│       ├── checks.rs
│       └── diff.rs
├── core/                 # Core library
│   ├── mod.rs
│   ├── manifest.rs       # Manifest parsing
│   ├── griptree.rs       # Griptree config and upstream tracking
│   ├── workspace.rs      # Workspace operations
│   ├── repo.rs           # Repo info and filtering
│   └── config.rs         # Configuration
├── git/                  # Git operations
│   ├── mod.rs
│   ├── repo.rs           # Repository operations
│   ├── branch.rs         # Branch operations
│   ├── remote.rs         # Remote/fetch/pull operations
│   ├── cache.rs          # Git status cache
│   └── worktree.rs       # Worktree operations
├── platform/             # Multi-platform hosting support
│   ├── mod.rs
│   ├── github.rs         # GitHub adapter
│   ├── gitlab.rs         # GitLab adapter
│   ├── azure.rs          # Azure DevOps adapter
│   ├── bitbucket.rs      # Bitbucket adapter
│   ├── rate_limit.rs     # API rate limiting
│   ├── traits.rs         # HostingPlatform trait
│   └── types.rs          # Shared platform types
├── files/                # File operations
│   └── mod.rs            # copyfile/linkfile
└── util/                 # Utilities
    ├── mod.rs
    ├── cmd.rs            # Command logging with credential sanitization
    ├── output.rs         # Colored output with progress bars
    ├── retry.rs          # Retry with exponential backoff
    └── timing.rs         # Benchmarking
```

## Key Concepts

### Manifest
Workspace configuration in `.gitgrip/spaces/main/gripspace.yml`:
- `repos`: Repository definitions with URL, path, default_branch
- `manifest`: Self-tracking for the manifest repo itself
- `gripspaces`: Composable includes — clone external gripspace repos and merge their repos, scripts, env, hooks, and file links
- `workspace`: Scripts, hooks, and environment variables
- `settings`: PR prefix, merge strategy


### Commands
All commands use `gr` (or `gitgrip`):
- `gr init <url>` - Initialize workspace from manifest URL
- `gr init --from-dirs` - Initialize workspace from existing local directories
- `gr init --from-repo` - Initialize from existing `.repo/` directory (git-repo coexistence)
- `gr sync` - Pull all repos in parallel + process links + run hooks (includes manifest)
  - `gr sync --sequential` - Sync repos sequentially (slower but ordered output)
  - `gr sync --group <name>` - Sync only repos in a group
  - `gr sync --repo <names>` - Sync only specific repos (use "manifest" to target manifest repo)
- `gr status` - Show repo and manifest status
  - `gr status --json` - Machine-readable output
- `gr branch <name>` - Create branch across all repos (includes manifest)
  - `gr branch --repo <names>` - Create branch in specific repos only
  - `gr branch --delete` - Delete branch across repos
  - `gr branch --move` - Move commits to new branch
- `gr checkout <branch>` - Checkout branch across all repos (includes manifest)
  - `gr checkout -b <branch>` - Create and checkout branch in one command
  - `gr checkout --base` - Checkout griptree base branch (griptree workspaces only)
- `gr add` - Stage changes across all repos
- `gr restore` - Unstage or discard changes across repos
  - `gr restore --staged` - Unstage files (remove from index)
  - `gr restore --repo <names>` - Only restore in specific repos
- `gr diff` - Show diff across all repos (includes manifest)
- `gr commit` - Commit staged changes across all repos
- `gr push` - Push current branch in all repos (includes manifest)
- `gr pull` - Pull latest changes across repos
  - `gr pull --rebase` - Rebase instead of merge
- `gr rebase` - Rebase across repos
  - `gr rebase --upstream` - Rebase onto upstream (uses griptree config if available)
  - `gr rebase --abort` / `--continue` - Manage in-progress rebases
- `gr pr create/status/merge/checks/diff` - Linked PR workflow
  - `gr pr create --push --draft` - Push and create as draft
  - `gr pr merge --update` - Update branch from base if behind, then merge
  - `gr pr merge --auto` - Enable auto-merge (merges when checks pass)
  - `gr pr merge --force` - Merge even if checks pending
- `gr repo add/list/remove` - Manage repositories
- `gr group list` - List all groups and their repos
  - `gr group add <group> <repos...>` - Add repos to a group
  - `gr group remove <group> <repos...>` - Remove repos from a group
- `gr target list` - Show current PR target branches
  - `gr target set <branch>` - Set global PR target branch
  - `gr target set <branch> --repo <name>` - Set target for a specific repo
  - `gr target unset` - Unset global target (falls back to revision)
  - `gr target unset --repo <name>` - Unset per-repo target (falls back to global)
- `gr grep <pattern>` - Search across all repos using git grep
  - `gr grep -i` - Case insensitive search
  - `gr grep --parallel` - Concurrent search
- `gr prune` - Clean up merged branches (dry-run by default)
  - `gr prune --execute` - Actually delete branches
  - `gr prune --remote` - Also prune remote tracking refs
- `gr gc` - Run garbage collection across repos
  - `gr gc --aggressive` - More thorough (slower)
  - `gr gc --dry-run` - Only report .git sizes
- `gr cherry-pick <commit>` - Cherry-pick commits across repos
  - `gr cherry-pick --abort` / `--continue` - Manage in-progress cherry-picks
- `gr ci run/list/status` - CI/CD pipeline operations
- `gr manifest import/sync/schema` - Manifest operations
  - `gr manifest schema` - Show manifest specification (YAML/JSON/Markdown)
- `gr link` - Manage copyfile/linkfile entries
- `gr run` - Execute workspace scripts
- `gr env` - Show workspace environment variables
- `gr bench` - Run benchmarks
- `gr forall -c "cmd"` - Run command in each repo
- `gr tree add/list/remove` - Manage griptrees (worktree-based multi-branch workspaces)
- `gr completions <shell>` - Generate shell completions (bash, zsh, fish, elvish, powershell)

### Griptrees (Multi-Branch Workspaces)

Griptrees allow you to work on multiple branches simultaneously without switching branches. Each griptree is a parallel workspace using git worktrees.

```bash
# Create a griptree for a feature branch
gr tree add feat/auth

# This creates a directory structure:
# ../feat-auth/
#   ├── codi/           # worktree of main/codi on feat/auth
#   ├── codi-private/   # worktree of main/codi-private on feat/auth
#   └── .gitgrip/spaces/main/  # worktree of manifest on feat/auth

# List all griptrees
gr tree list

# Lock a griptree to prevent accidental removal
gr tree lock feat/auth

# Return to griptree base branch
gr checkout --base

# Remove a griptree (removes worktrees, not branches)
gr tree remove feat/auth
```

**Upstream Tracking:**

Each griptree records per-repo upstream defaults in `.gitgrip/griptree.json`. This allows repos in the same workspace to track different upstream branches (e.g., one repo tracks `origin/main`, another tracks `origin/dev`).

- `gr tree add` auto-detects upstream for each repo
- `gr sync` uses per-repo upstream when on the griptree base branch
- `gr rebase --upstream` rebases each repo onto its configured upstream
- Falls back to `origin/<default_branch>` when no upstream is configured

**Benefits:**
- No branch switching - work on multiple features in parallel
- Shared git objects - worktrees share `.git/objects` with main
- Faster than cloning - worktree creation is nearly instant
- Per-repo upstream tracking - different repos can track different branches

**Limitations:**
- Branch exclusivity - can't checkout same branch in two worktrees
- Separate dependencies - each worktree needs own dependencies

### File Linking
- `copyfile`: Copy file from repo to workspace
- `linkfile`: Create symlink from workspace to repo
- Path validation prevents directory traversal

### Multi-Platform Support

gitgrip supports multiple hosting platforms:
- **GitHub** (github.com and GitHub Enterprise)
- **GitLab** (gitlab.com and self-hosted)
- **Azure DevOps** (dev.azure.com and Azure DevOps Server)
- **Bitbucket** (bitbucket.org and self-hosted)

**Platform Detection:**
- Platform is auto-detected from git URLs
- Can be overridden in manifest with `platform:` config

**Example mixed-platform manifest:**
```yaml
repos:
  frontend:
    url: git@github.com:org/frontend.git
    path: ./frontend
  backend:
    url: git@gitlab.com:org/backend.git
    path: ./backend
  infra:
    url: https://dev.azure.com/org/project/_git/infra
    path: ./infra
  legacy:
    url: git@bitbucket.org:org/legacy.git
    path: ./legacy
```

**Platform Architecture:**
- `HostingPlatform` trait defines all platform operations
- Each platform has an adapter in `src/platform/`
- Platform adapters handle: PR create/merge/status, reviews, status checks, URL parsing
- Rate limiting infrastructure for API calls

**Adding a New Platform:**
1. Create adapter in `src/platform/newplatform.rs`
2. Implement `HostingPlatform` trait
3. Add detection logic in `src/platform/mod.rs`
4. Add tests

## Testing

```bash
cargo test                      # Run all tests
cargo test -- --nocapture       # Show output
cargo test manifest             # Filter by name
```

Test files are alongside the modules they test or in `tests/`.

## Adding a New Command

1. Create `src/cli/mycommand.rs`
2. Add command to CLI in `src/main.rs`
3. Implement the command handler
4. Add tests

## Code Style

- Follow Rust idioms
- Use `anyhow` for error handling
- Use `colored` for colored output
- Validate manifest schema in `core/manifest.rs`

## Continuous Improvement

gitgrip is self-improving. When you encounter friction, missing features, or bugs:

1. **Tell the user** about the friction point
2. **Ask if they want a GitHub issue created** - e.g., "Want me to create an issue for this?"
3. **Create the issue directly** with `gh issue create` if approved

No need to document in IMPROVEMENTS.md first - create issues directly to reduce overhead.

### CRITICAL: Report When Falling Back to Raw Commands

**If you ever use raw `git` or `gh` commands, you MUST immediately:**

1. Tell the user about the friction point
2. Ask if they want a GitHub issue created

This is mandatory. Every workaround reveals a gap in `gr` that should be fixed.
