//! CLI argument types and command definitions

use clap::{Parser, Subcommand};
use clap_complete::Shell;

use crate::platform::MergeMethod;

/// Filter for listing pull requests
#[derive(Debug, Clone, Copy, clap::ValueEnum)]
pub enum PrStateFilter {
    Open,
    Closed,
    Merged,
    All,
}

#[derive(Parser)]
#[command(name = "gr")]
#[command(author, version, about = "Multi-repo workflow tool", long_about = None)]
pub struct Cli {
    /// Suppress output for repos with no relevant changes (saves tokens for AI tools)
    #[arg(short, long, global = true)]
    pub quiet: bool,

    /// Show verbose output including external commands being executed
    #[arg(short, long, global = true)]
    pub verbose: bool,

    /// Output in JSON format (machine-readable)
    #[arg(long, global = true)]
    pub json: bool,

    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Subcommand)]
pub enum Commands {
    /// Initialize a new workspace
    Init {
        /// Manifest URL
        url: Option<String>,
        /// Target directory
        #[arg(short, long)]
        path: Option<String>,
        /// Create workspace from existing local directories
        #[arg(long, conflicts_with_all = ["url", "from_repo"])]
        from_dirs: bool,
        /// Specific directories to scan (requires --from-dirs)
        #[arg(long, requires = "from_dirs")]
        dirs: Vec<String>,
        /// Interactive mode - preview and confirm before writing (default: auto-detect from TTY)
        #[arg(short, long)]
        interactive: bool,
        /// Disable interactive mode (overrides TTY auto-detection)
        #[arg(long, conflicts_with = "interactive")]
        no_interactive: bool,
        /// Create manifest repository on detected platform (requires --from-dirs)
        #[arg(long, requires = "from_dirs")]
        create_manifest: bool,
        /// Name for manifest repository (default: workspace-manifest)
        #[arg(long, requires = "create_manifest")]
        manifest_name: Option<String>,
        /// Make manifest repository private (default: false)
        #[arg(long, requires = "create_manifest")]
        private: bool,
        /// Initialize from existing .repo/ directory (git-repo coexistence)
        #[arg(long, conflicts_with_all = ["from_dirs", "url"])]
        from_repo: bool,
    },
    /// Migrate existing repos into a new gripspace
    Migrate {
        #[command(subcommand)]
        action: MigrateCommands,
    },
    /// Sync all repositories
    Sync {
        /// Force sync even with local changes
        #[arg(short, long)]
        force: bool,
        /// Hard reset reference repos to upstream (discard local changes)
        #[arg(long, alias = "reset-ref")]
        reset_refs: bool,
        /// Only sync repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
        /// Only sync specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Sync repos sequentially (default: parallel)
        #[arg(long)]
        sequential: bool,
        /// Skip post-sync hooks
        #[arg(long)]
        no_hooks: bool,
        /// Rollback repos to their state before the last sync
        #[arg(long, conflicts_with_all = ["force", "reset_refs", "group", "repo", "sequential", "no_hooks"])]
        rollback: bool,
    },
    /// Show status of all repositories
    Status {
        /// Show detailed status
        #[arg(short, long)]
        verbose: bool,
        /// Only show repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
        /// Only show specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
    },
    /// Create or switch branches across repos
    Branch {
        /// Branch name
        name: Option<String>,
        /// Delete branch
        #[arg(short, long)]
        delete: bool,
        /// Move recent commits to new branch (resets current branch to remote)
        #[arg(short, long)]
        r#move: bool,
        /// Only operate on specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only operate on repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Checkout a branch across repos
    Checkout {
        /// Branch name
        name: Option<String>,
        /// Create branch if it doesn't exist
        #[arg(short = 'b', long)]
        create: bool,
        /// Checkout the griptree base branch for this worktree
        #[arg(long, conflicts_with = "create")]
        base: bool,
        /// Only operate on specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only operate on repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Stage changes across repos
    Add {
        /// Files to add (. for all)
        #[arg(default_value = ".")]
        files: Vec<String>,
        /// Only stage in specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only stage in repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Unstage or discard changes across repos
    Restore {
        /// Files to restore (. for all)
        #[arg(default_value = ".")]
        files: Vec<String>,
        /// Unstage files (remove from index)
        #[arg(long)]
        staged: bool,
        /// Only restore in specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only restore in repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Show diff across repos
    Diff {
        /// Show staged changes
        #[arg(long)]
        staged: bool,
        /// Only diff specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only diff repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Commit changes across repos
    Commit {
        /// Commit message
        #[arg(short, long)]
        message: Option<String>,
        /// Amend previous commit
        #[arg(long)]
        amend: bool,
        /// Only commit in specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only commit in repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Push changes across repos
    Push {
        /// Set upstream
        #[arg(short = 'u', long)]
        set_upstream: bool,
        /// Force push
        #[arg(short, long)]
        force: bool,
        /// Only push specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only push repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Clean up merged branches across repos
    Prune {
        /// Actually delete branches (default: dry-run)
        #[arg(long)]
        execute: bool,
        /// Also prune remote tracking refs
        #[arg(long)]
        remote: bool,
        /// Only prune specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only prune repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Pull request operations
    Pr {
        #[command(subcommand)]
        action: PrCommands,
    },
    /// Griptree operations — manage worktree-based checkouts that share the gripspace
    Tree {
        #[command(subcommand)]
        action: TreeCommands,
    },
    /// Search across all repos using git grep
    Grep {
        /// Search pattern
        pattern: String,
        /// Case insensitive
        #[arg(short = 'i', long)]
        ignore_case: bool,
        /// Run in parallel
        #[arg(short, long)]
        parallel: bool,
        /// File pattern (after --)
        #[arg(last = true)]
        pathspec: Vec<String>,
        /// Only search specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only search repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Run command in each repo
    Forall {
        /// Command to run
        #[arg(short, long)]
        command: String,
        /// Run in parallel
        #[arg(short, long)]
        parallel: bool,
        /// Run in ALL repos (default: only repos with changes)
        #[arg(short, long)]
        all: bool,
        /// Disable git command interception (use CLI for all commands)
        #[arg(long)]
        no_intercept: bool,
        /// Only run in specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only run in repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Rebase branches across repos
    Rebase {
        /// Target branch
        onto: Option<String>,
        /// Use upstream tracking branch when no target is provided
        #[arg(long)]
        upstream: bool,
        /// Abort rebase in progress
        #[arg(long)]
        abort: bool,
        /// Continue rebase after resolving conflicts
        #[arg(long, name = "continue")]
        continue_rebase: bool,
        /// Only rebase specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only rebase repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Pull latest changes across repos
    Pull {
        /// Rebase instead of merge
        #[arg(long)]
        rebase: bool,
        /// Only pull specific repos (use "manifest" to target manifest repo)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only pull repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
        /// Sync repos sequentially (default: parallel)
        #[arg(long)]
        sequential: bool,
    },
    /// Manage file links
    Link {
        /// Show link status
        #[arg(long)]
        status: bool,
        /// Apply/fix links
        #[arg(long)]
        apply: bool,
    },
    /// Run workspace scripts
    Run {
        /// Script name
        name: Option<String>,
        /// List available scripts
        #[arg(long)]
        list: bool,
    },
    /// Show environment variables
    Env,
    /// Run benchmarks
    Bench(crate::cli::commands::bench::BenchArgs),
    /// Repository operations
    Repo {
        #[command(subcommand)]
        action: RepoCommands,
    },
    /// Repository group operations
    Group {
        #[command(subcommand)]
        action: GroupCommands,
    },
    /// View or set the PR target branch (base branch)
    Target {
        #[command(subcommand)]
        action: TargetCommands,
    },
    /// Manage workspace repo caches (.grip/cache/)
    Cache {
        #[command(subcommand)]
        action: CacheCommands,
    },
    /// Run garbage collection across repos
    Gc {
        /// More thorough gc (slower)
        #[arg(long)]
        aggressive: bool,
        /// Only report .git sizes, don't gc
        #[arg(long)]
        dry_run: bool,
        /// Only operate on specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only gc repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Cherry-pick commits across repos
    CherryPick {
        /// Commit SHA to cherry-pick
        #[arg(conflicts_with_all = ["abort", "continue"])]
        commit: Option<String>,
        /// Abort in-progress cherry-pick
        #[arg(long, conflicts_with = "continue")]
        abort: bool,
        /// Continue after conflict resolution
        #[arg(long, name = "continue", conflicts_with = "abort")]
        continue_pick: bool,
        /// Only operate on specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only operate on repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// CI/CD pipeline operations
    Ci {
        #[command(subcommand)]
        action: CiCommands,
    },
    /// Manifest operations (import, sync)
    Manifest {
        #[command(subcommand)]
        action: ManifestCommands,
    },
    /// AI agent operations (context, build, test, verify)
    Agent {
        #[command(subcommand)]
        action: AgentCommands,
    },
    /// Multi-agent spawn and orchestration
    Spawn {
        #[command(subcommand)]
        action: SpawnCommands,
    },
    /// Communicate with agents via recall channels
    Channel {
        #[command(subcommand)]
        action: ChannelCommands,
    },
    /// Issue operations
    Issue {
        #[command(subcommand)]
        action: IssueCommands,
    },
    /// MCP server operations
    Mcp {
        #[command(subcommand)]
        action: McpCommands,
    },
    /// Automated release workflow
    Release {
        /// Version to release (e.g. v0.12.4)
        version: String,
        /// Release notes
        #[arg(short, long)]
        notes: Option<String>,
        /// Show what would happen without doing it
        #[arg(long)]
        dry_run: bool,
        /// Skip PR workflow (bump, tag, release only)
        #[arg(long)]
        skip_pr: bool,
        /// Target repo for GitHub release (default: auto-detect)
        #[arg(long)]
        repo: Option<String>,
        /// Timeout in seconds for CI wait (default: 600)
        #[arg(long, default_value = "600")]
        timeout: u64,
    },
    /// Generate shell completions
    Completions {
        /// Shell to generate completions for
        #[arg(value_enum)]
        shell: Shell,
    },
    /// Verify workspace assertions (exit 0 = pass, 1 = fail)
    Verify {
        /// All repos are clean (no uncommitted changes)
        #[arg(long)]
        clean: bool,
        /// All copyfile/linkfile entries are valid
        #[arg(long)]
        links: bool,
        /// All non-reference repos are on this branch
        #[arg(long, value_name = "BRANCH")]
        on_branch: Option<String>,
        /// All repos are synced with remote (not ahead/behind)
        #[arg(long)]
        synced: bool,
        /// Only verify specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Only verify repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
}

#[derive(Subcommand)]
pub enum AgentCommands {
    /// Dump workspace context for AI agent system prompts
    Context {
        /// Filter to a specific repo
        #[arg(long)]
        repo: Option<String>,
    },
    /// Build repo(s) using manifest agent config
    Build {
        /// Specific repo to build (default: all with agent.build)
        repo: Option<String>,
    },
    /// Test repo(s) using manifest agent config
    Test {
        /// Specific repo to test (default: all with agent.test)
        repo: Option<String>,
    },
    /// Run all verification checks (build + test + lint)
    Verify {
        /// Specific repo to verify (default: all with agent config)
        repo: Option<String>,
    },
    /// Generate context files for all configured AI tool targets
    GenerateContext {
        /// Show what would be generated without writing files
        #[arg(long)]
        dry_run: bool,
    },
}

#[derive(Subcommand)]
pub enum SpawnCommands {
    /// Launch all agents (or a specific agent)
    Up {
        /// Launch only this agent
        #[arg(long)]
        agent: Option<String>,
        /// Path to agents.toml (default: .gitgrip/agents.toml)
        #[arg(long)]
        config: Option<String>,
        /// Force mock mode regardless of config
        #[arg(long)]
        mock: bool,
    },
    /// Show agent status (tmux + heartbeat)
    Status,
    /// Stop all agents (or a specific agent)
    Down {
        /// Stop only this agent
        agent: Option<String>,
    },
    /// List configured agents
    List,
    /// Attach to an agent's tmux window
    Attach {
        /// Agent name
        agent: String,
    },
    /// View agent output without attaching
    Logs {
        /// Agent name (required unless --all)
        agent: Option<String>,
        /// Number of lines to show
        #[arg(short = 'n', long, default_value = "50")]
        lines: u32,
        /// Show logs from all running agents
        #[arg(long)]
        all: bool,
    },
    /// Open mission control dashboard (2x2 agent grid + #dev input)
    Dashboard,
    /// Open web dashboard in browser (synapt dashboard)
    Web {
        /// Port for the web dashboard
        #[arg(long, default_value = "8420")]
        port: u16,
        /// Don't auto-open browser
        #[arg(long)]
        no_open: bool,
    },
}

#[derive(Subcommand)]
pub enum ChannelCommands {
    /// Post a message to a channel
    Post {
        /// Message body
        message: String,
        /// Channel name
        #[arg(short, long, default_value = "dev")]
        channel: String,
        /// Pin this message
        #[arg(long)]
        pin: bool,
    },
    /// Read recent messages
    Read {
        /// Channel name
        #[arg(default_value = "dev")]
        channel: String,
        /// Max messages to return
        #[arg(short, long, default_value = "20")]
        limit: u32,
        /// Output detail level (max/high/medium/low/min)
        #[arg(short, long, default_value = "medium")]
        detail: String,
    },
    /// Show who's online
    Who,
    /// Search across channel history
    Search {
        /// Search query
        query: String,
        /// Channel to search (all channels if omitted)
        #[arg(short, long)]
        channel: Option<String>,
    },
    /// List all channels
    List,
    /// Join a channel with a display name
    Join {
        /// Channel name
        #[arg(default_value = "dev")]
        channel: String,
        /// Display name for this agent
        #[arg(long)]
        name: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum IssueCommands {
    /// List issues
    List {
        /// Target repo (required when workspace has multiple repos with remotes)
        #[arg(long)]
        repo: Option<String>,
        /// Filter by state (open, closed, all)
        #[arg(long, default_value = "open")]
        state: String,
        /// Filter by labels (comma-separated)
        #[arg(long, value_delimiter = ',')]
        label: Option<Vec<String>>,
        /// Filter by assignee
        #[arg(long)]
        assignee: Option<String>,
        /// Maximum number of issues to show (1-100)
        #[arg(long, default_value = "30", value_parser = clap::value_parser!(u32).range(1..=100))]
        limit: u32,
    },
    /// Create a new issue
    Create {
        /// Target repo (required when workspace has multiple repos with remotes)
        #[arg(long)]
        repo: Option<String>,
        /// Issue title
        #[arg(short, long)]
        title: String,
        /// Issue body/description
        #[arg(short, long)]
        body: Option<String>,
        /// Labels (comma-separated)
        #[arg(short, long, value_delimiter = ',')]
        label: Option<Vec<String>>,
        /// Assignees (comma-separated)
        #[arg(short, long, value_delimiter = ',')]
        assignee: Option<Vec<String>>,
    },
    /// View issue details
    View {
        /// Issue number
        #[arg(value_parser = clap::value_parser!(u64).range(1..))]
        number: u64,
        /// Target repo (required when workspace has multiple repos with remotes)
        #[arg(long)]
        repo: Option<String>,
    },
    /// Close an issue
    Close {
        /// Issue number
        #[arg(value_parser = clap::value_parser!(u64).range(1..))]
        number: u64,
        /// Target repo (required when workspace has multiple repos with remotes)
        #[arg(long)]
        repo: Option<String>,
    },
    /// Reopen an issue
    Reopen {
        /// Issue number
        #[arg(value_parser = clap::value_parser!(u64).range(1..))]
        number: u64,
        /// Target repo (required when workspace has multiple repos with remotes)
        #[arg(long)]
        repo: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum McpCommands {
    /// Start stdio MCP server exposing gitgrip tools
    Server,
}

#[derive(Subcommand)]
pub enum PrCommands {
    /// Create a pull request
    Create {
        /// PR title
        #[arg(short, long)]
        title: Option<String>,
        /// PR body/description
        #[arg(short, long)]
        body: Option<String>,
        /// Push before creating
        #[arg(long)]
        push: bool,
        /// Create as draft
        #[arg(long)]
        draft: bool,
        /// Preview without creating PR
        #[arg(long)]
        dry_run: bool,
        /// Only create PRs for specific repos (comma-separated)
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
    },
    /// Show PR status
    Status,
    /// Merge pull requests
    Merge {
        /// Merge method (merge, squash, rebase)
        #[arg(long, value_enum)]
        method: Option<MergeMethod>,
        /// Force merge without readiness checks
        #[arg(short, long)]
        force: bool,
        /// Update branch from base if behind before merging
        #[arg(short = 'u', long)]
        update: bool,
        /// Enable auto-merge (merges when all checks pass)
        #[arg(long)]
        auto: bool,
        /// Wait for checks to pass before merging
        #[arg(short = 'w', long)]
        wait: bool,
        /// Timeout in seconds for --wait (default: 600)
        #[arg(long, default_value = "600")]
        timeout: u64,
        /// Don't delete the source branch after merging
        #[arg(long)]
        no_delete_branch: bool,
    },
    /// Edit pull request title/body
    Edit {
        /// New PR title
        #[arg(short, long)]
        title: Option<String>,
        /// New PR body/description
        #[arg(short, long)]
        body: Option<String>,
    },
    /// Check CI status
    Checks {
        /// Filter to a specific repo
        #[arg(long)]
        repo: Option<String>,
    },
    /// Show PR diff
    Diff {
        /// Show stat summary only
        #[arg(long)]
        stat: bool,
    },
    /// List pull requests
    List {
        /// Filter by state
        #[arg(long, value_enum, default_value = "open")]
        state: PrStateFilter,
        /// Filter to a specific repo
        #[arg(long)]
        repo: Option<String>,
        /// Maximum number of PRs per repo
        #[arg(long, default_value = "30", value_parser = clap::value_parser!(u32).range(1..=100))]
        limit: u32,
    },
    /// View pull request details
    View {
        /// PR number (omit to find PR for current branch)
        number: Option<u64>,
        /// Filter to a specific repo
        #[arg(long)]
        repo: Option<String>,
    },
    /// Review pull requests (approve, request changes, or comment)
    Review {
        /// Review action
        #[arg(value_enum)]
        event: crate::platform::ReviewEvent,
        /// Review comment body (required for comment and request-changes)
        #[arg(short, long)]
        body: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum TreeCommands {
    /// Add a new griptree
    Add {
        /// Branch name
        branch: String,
    },
    /// List griptrees
    List,
    /// Remove a griptree
    Remove {
        /// Branch name
        branch: String,
        /// Force removal
        #[arg(short, long)]
        force: bool,
    },
    /// Lock a griptree
    Lock {
        /// Branch name
        branch: String,
        /// Lock reason
        #[arg(short, long)]
        reason: Option<String>,
    },
    /// Unlock a griptree
    Unlock {
        /// Branch name
        branch: String,
    },
    /// Return to the griptree base branch, sync, and optionally prune a branch
    Return {
        /// Override base branch (defaults to griptree config)
        #[arg(long)]
        base: Option<String>,
        /// Skip syncing after checkout
        #[arg(long)]
        no_sync: bool,
        /// Stash and restore local changes automatically
        #[arg(long)]
        autostash: bool,
        /// Prune this branch after returning
        #[arg(long)]
        prune: Option<String>,
        /// Prune the current branch (pre-return) after returning
        #[arg(long, conflicts_with = "prune")]
        prune_current: bool,
        /// Also prune the remote branch (origin)
        #[arg(long)]
        prune_remote: bool,
        /// Force delete local branches even if not merged
        #[arg(short, long)]
        force: bool,
    },
}

#[derive(Subcommand)]
pub enum GroupCommands {
    /// List all groups and their repos
    List,
    /// Add repo(s) to a group
    Add {
        /// Group name
        group: String,
        /// Repository names
        #[arg(required = true)]
        repos: Vec<String>,
    },
    /// Remove repo(s) from a group
    Remove {
        /// Group name
        group: String,
        /// Repository names
        #[arg(required = true)]
        repos: Vec<String>,
    },
    /// Create a new empty group (for documentation purposes)
    Create {
        /// Group name
        name: String,
    },
}

#[derive(Subcommand)]
pub enum TargetCommands {
    /// Show current target branches for all repos
    List,
    /// Set the global target branch
    Set {
        /// Branch name
        branch: String,
        /// Set target for a specific repo instead of globally
        #[arg(long)]
        repo: Option<String>,
    },
    /// Unset the target (fall back to revision/default)
    Unset {
        /// Unset target for a specific repo instead of globally
        #[arg(long)]
        repo: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum CacheCommands {
    /// Bootstrap bare caches for all manifest repos
    Bootstrap,
    /// Fetch latest refs into all caches
    Update,
    /// Show cache status
    Status,
    /// Remove a repo cache
    Remove {
        /// Repo name
        repo: String,
    },
}

#[derive(Subcommand)]
pub enum CiCommands {
    /// Run a CI pipeline
    Run {
        /// Pipeline name
        name: String,
    },
    /// List available pipelines
    List,
    /// Show status of last CI runs
    Status,
}

#[derive(Subcommand)]
pub enum ManifestCommands {
    /// Convert git-repo XML manifest to gitgrip YAML
    Import {
        /// Path to XML manifest (e.g., .repo/manifests/default.xml)
        path: String,
        /// Output path for YAML manifest
        #[arg(short, long)]
        output: Option<String>,
    },
    /// Re-sync gitgrip YAML from .repo/ manifest after repo sync
    Sync,
    /// Show manifest schema specification
    Schema {
        /// Output format (yaml, json, markdown)
        #[arg(long, default_value = "yaml")]
        format: String,
    },
}

#[derive(Subcommand)]
pub enum RepoCommands {
    /// List repositories
    List,
    /// Add a repository
    Add {
        /// Repository URL
        url: String,
        /// Local path
        #[arg(short, long)]
        path: Option<String>,
        /// Default branch
        #[arg(short, long)]
        branch: Option<String>,
        /// Workflow target branch (remote/branch format, e.g. "origin/develop")
        #[arg(short, long)]
        target: Option<String>,
    },
    /// Remove a repository
    Remove {
        /// Repository name
        name: String,
        /// Delete files from disk
        #[arg(long)]
        delete: bool,
    },
}

#[derive(Subcommand)]
pub enum MigrateCommands {
    /// Generate a gripspace from existing GitHub repos
    FromRepos {
        /// GitHub repos to include (format: owner/repo)
        #[arg(long = "repo", required = true)]
        repos: Vec<String>,
        /// GitHub org for manifest + config repos
        #[arg(long)]
        org: Option<String>,
        /// Prefix for manifest/config repo names
        #[arg(long)]
        prefix: Option<String>,
        /// Target directory for the gripspace
        #[arg(short, long)]
        path: Option<String>,
    },
    /// Convert an existing git repo directory into a gripspace in-place
    ///
    /// Moves the repo contents into a child directory (named after the repo),
    /// keeps .synapt/ and .claude/ at the gripspace root, and repairs any
    /// linked worktree paths. Requires git 2.30+.
    ///
    /// Example: gr migrate in-place
    ///   ~/conversa/           → ~/conversa/conversa-app/ (repo)
    ///                            ~/conversa/.synapt/     (stays)
    ///                            ~/conversa/.claude/     (stays)
    InPlace {
        /// Show what would happen without making any changes
        #[arg(long)]
        dry_run: bool,
        /// Path to the repo directory (default: current directory)
        #[arg(short, long)]
        path: Option<String>,
    },
}
