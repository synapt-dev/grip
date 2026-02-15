# P3 Extensibility — Implementation Plan

*February 2026 — 5 PRs covering the extensibility action items from `docs/ARCHITECTURAL_ASSESSMENT.md`*

## Context

P0 (safety), P1 (quality), and P2 (maintainability) are merged. P3 is the final tier from `docs/ARCHITECTURAL_ASSESSMENT.md` — 5 extensibility items that improve testability, reduce build overhead, and add operational safety. Work happens on `main` in gitgrip, one PR per item.

## PR 1: Trim tokio features (`chore/tokio-trim`)

**Why:** `features = ["full"]` pulls in ~15 feature flags. The codebase only uses 4.

**Change:** `Cargo.toml:44`
```toml
# Before:
tokio = { version = "1", features = ["full"] }
# After:
tokio = { version = "1", features = ["rt-multi-thread", "macros", "process", "time"] }
```

**Actual usage found:**
- `rt-multi-thread` — `JoinSet::spawn_blocking` in sync.rs, pull.rs
- `macros` — `#[tokio::main]`, `#[tokio::test]`
- `process` — `tokio::process::Command` in github.rs, gitlab.rs, azure.rs
- `time` — `tokio::time::sleep` in retry.rs, pr/merge.rs, rate_limit.rs

**Verify:** `cargo build && cargo test && cargo clippy`

---

## PR 2: Platform capability matrix (`docs/platform-capabilities`)

**Why:** Users and contributors need to know which operations each platform supports without reading source.

**Changes:**

1. New `src/platform/capabilities.rs`:
   ```rust
   pub enum PlatformCapability {
       CreatePr, MergePr, FindPrByBranch, GetReviews,
       StatusChecks, AutoMerge, UpdateBranch,
       CreateRepo, DeleteRepo, CreateRelease,
   }

   pub fn platform_capabilities(platform: PlatformType) -> Vec<PlatformCapability> {
       // Return capabilities based on what each adapter actually implements
       // (vs returning Err(Unsupported) in the default trait methods)
   }
   ```

2. Add `capabilities()` default method to `HostingPlatform` trait in `src/platform/traits.rs`

3. New `docs/PLATFORM_CAPABILITIES.md` — generated table:

   | Capability | GitHub | GitLab | Azure | Bitbucket |
   |------------|--------|--------|-------|-----------|
   | Create PR | Yes | Yes | Yes | Yes |
   | Auto-merge | Yes | No | No | No |
   | Update branch | Yes | No | No | No |
   | Create release | Yes | No | No | No |
   | ... | ... | ... | ... | ... |

4. Wire into `src/platform/mod.rs` module declarations

**Verify:** `cargo build && cargo test`; spot-check that `platform_capabilities(GitHub)` returns expected set

---

## PR 3: OutputSink trait (`refactor/output-sink`)

**Why:** `Output` is all static methods printing directly to stdout. Tests can't capture output, quiet mode is checked ad-hoc per command, and JSON mode requires separate code paths.

**Changes:**

1. New `src/cli/output_sink.rs`:
   ```rust
   pub trait OutputSink: Send + Sync {
       fn success(&self, msg: &str);
       fn error(&self, msg: &str);
       fn warning(&self, msg: &str);
       fn info(&self, msg: &str);
       fn header(&self, msg: &str);
       fn kv(&self, key: &str, value: &str);
       fn is_quiet(&self) -> bool;
       fn is_json(&self) -> bool;
   }

   pub struct TerminalSink { quiet: bool, json: bool }
   pub struct BufferSink { buffer: Arc<Mutex<Vec<String>>> }
   ```

2. `TerminalSink` delegates to existing `Output::*` static methods (no behavior change)

3. `BufferSink` captures output for testing

4. Add `sink: Arc<dyn OutputSink>` to `WorkspaceContext` in `src/cli/context.rs`

5. Construct `TerminalSink` in `load_workspace_context()` in `src/main.rs`

6. **No command migration in this PR** — just the infrastructure. Commands can adopt `ctx.sink` incrementally.

**Files:**
- `src/cli/output_sink.rs` — NEW
- `src/cli/mod.rs` — add module
- `src/cli/context.rs` — add `sink` field
- `src/main.rs` — construct sink in `load_workspace_context()`

**Verify:** `cargo build && cargo test`; unit tests for `BufferSink` capturing output

---

## PR 4: GitBackend trait (`refactor/git-backend`)

**Why:** All git ops are free functions in `src/git/`. Can't swap implementations (git2 vs gitoxide), can't mock in tests without filesystem.

**Changes:**

1. New `src/git/backend.rs` — trait covering the core operations:
   ```rust
   pub trait GitBackend: Send + Sync {
       fn open_repo(&self, path: &Path) -> Result<Box<dyn GitRepo>, GitError>;
       fn clone_repo(&self, url: &str, path: &Path, branch: Option<&str>) -> Result<Box<dyn GitRepo>, GitError>;
       fn is_git_repo(&self, path: &Path) -> bool;
   }

   pub trait GitRepo: Send + Sync {
       fn current_branch(&self) -> Result<String, GitError>;
       fn checkout_branch(&self, name: &str, create: bool) -> Result<(), GitError>;
       fn create_branch(&self, name: &str, from: Option<&str>) -> Result<(), GitError>;
       fn delete_branch(&self, name: &str) -> Result<(), GitError>;
       fn fetch(&self, remote: &str) -> Result<(), GitError>;
       fn pull(&self, remote: &str, branch: &str) -> Result<PullResult, GitError>;
       fn push(&self, remote: &str, branch: &str, force: bool) -> Result<(), GitError>;
       fn head_commit_id(&self) -> Result<String, GitError>;
       fn status(&self) -> Result<RepoStatus, GitError>;
       fn workdir(&self) -> &Path;
   }
   ```

2. New `src/git/git2_backend.rs` — wraps existing free functions:
   ```rust
   pub struct Git2Backend;

   impl GitBackend for Git2Backend {
       fn open_repo(&self, path: &Path) -> Result<Box<dyn GitRepo>, GitError> {
           let repo = git::open_repo(path)?;
           Ok(Box::new(Git2Repo { repo }))
       }
       // ...delegates to existing src/git/*.rs functions
   }
   ```

3. Add `git_backend: Arc<dyn GitBackend>` to `WorkspaceContext` in `src/cli/context.rs`

4. Default to `Git2Backend` in `load_workspace_context()` in `src/main.rs`

5. **No command migration in this PR** — commands continue calling `git::open_repo()` directly. The trait exists for future use and for new code to adopt.

**Files:**
- `src/git/backend.rs` — NEW (trait definitions)
- `src/git/git2_backend.rs` — NEW (implementation wrapping existing functions)
- `src/git/mod.rs` — add modules
- `src/cli/context.rs` — add `git_backend` field
- `src/main.rs` — construct backend

**Verify:** `cargo build && cargo test`; unit test that `Git2Backend` can open a repo and read HEAD

---

## PR 5: Multi-repo rollback (`feat/sync-rollback`)

**Why:** `gr sync` pulls across all repos. If repo 3/5 fails, repos 1-2 are already updated with no way back.

**Changes:**

1. New `src/core/sync_state.rs`:
   ```rust
   pub struct SyncSnapshot {
       pub timestamp: DateTime<Utc>,
       pub repos: Vec<RepoSnapshot>,
   }

   pub struct RepoSnapshot {
       pub name: String,
       pub path: PathBuf,
       pub head_commit: String,
       pub branch: String,
   }

   impl SyncSnapshot {
       pub fn capture(workspace_root: &Path, repos: &[RepoInfo]) -> Result<Self>;
       pub fn save(&self, workspace_root: &Path) -> Result<()>;
       pub fn load_latest(workspace_root: &Path) -> Result<Option<Self>>;
   }
   ```

2. State file: `.gitgrip/sync-state.json` — written before sync, read by rollback

3. In `src/cli/commands/sync.rs`:
   - Before sync: `SyncSnapshot::capture()` records each repo's HEAD commit
   - After sync: state file persists for potential rollback
   - New `--rollback` flag: reads last snapshot, resets each repo to recorded HEAD

4. Add `--rollback` flag to `Sync` subcommand in `src/main.rs`

5. New `run_sync_rollback()` function in sync.rs:
   ```rust
   fn run_sync_rollback(workspace_root: &Path, repos: &[RepoInfo], quiet: bool) -> Result<()> {
       let snapshot = SyncSnapshot::load_latest(workspace_root)?
           .ok_or_else(|| anyhow!("No sync snapshot found"))?;
       for repo_snap in &snapshot.repos {
           // git checkout <branch> && git reset --hard <commit>
       }
   }
   ```

**Files:**
- `src/core/sync_state.rs` — NEW
- `src/core/mod.rs` — add module
- `src/cli/commands/sync.rs` — capture snapshot before sync, add rollback path
- `src/main.rs` — add `--rollback` flag to Sync subcommand

**Verify:** `cargo build && cargo test`; manual test: `gr sync`, check `.gitgrip/sync-state.json` exists, `gr sync --rollback` restores HEADs

---

## Execution Order

1. **PR 1** (tokio trim) — 1 file, ~0 risk, immediate compile-time win
2. **PR 2** (platform capabilities) — docs + small module, no behavior change
3. **PR 3** (OutputSink) — infrastructure only, no command changes
4. **PR 4** (GitBackend) — infrastructure only, no command changes
5. **PR 5** (sync rollback) — new feature, modifies sync flow

PRs 1-2 are independent. PRs 3-4 depend on P2's `WorkspaceContext`. PR 5 is standalone.

## Verification (all PRs)

For each PR:
1. `cargo build` — compiles clean
2. `cargo test` — all tests pass
3. `cargo clippy` — no new warnings
4. `cargo fmt --check` — formatted
5. CI checks pass before merge
