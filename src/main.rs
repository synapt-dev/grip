//! gitgrip CLI entry point

use clap::{CommandFactory, Parser, Subcommand};
use clap_complete::{generate, Shell};
use colored::Colorize;
use gitgrip::platform::MergeMethod;

#[derive(Parser)]
#[command(name = "gr")]
#[command(author, version, about = "Multi-repo workflow tool", long_about = None)]
struct Cli {
    /// Suppress output for repos with no relevant changes (saves tokens for AI tools)
    #[arg(short, long, global = true)]
    quiet: bool,

    /// Show verbose output including external commands being executed
    #[arg(short, long, global = true)]
    verbose: bool,

    /// Output in JSON format (machine-readable)
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
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
        /// Interactive mode - preview and confirm before writing
        #[arg(short, long)]
        interactive: bool,
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
        /// Sync repos sequentially (default: parallel)
        #[arg(long)]
        sequential: bool,
        /// Skip post-sync hooks
        #[arg(long)]
        no_hooks: bool,
        /// Rollback repos to their state before the last sync
        #[arg(long, conflicts_with_all = ["force", "reset_refs", "group", "sequential", "no_hooks"])]
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
        /// Only operate on specific repos
        #[arg(long, value_delimiter = ',')]
        repo: Option<Vec<String>>,
        /// Include manifest repo
        #[arg(long)]
        include_manifest: bool,
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
    },
    /// Stage changes across repos
    Add {
        /// Files to add (. for all)
        #[arg(default_value = ".")]
        files: Vec<String>,
    },
    /// Show diff across repos
    Diff {
        /// Show staged changes
        #[arg(long)]
        staged: bool,
    },
    /// Commit changes across repos
    Commit {
        /// Commit message
        #[arg(short, long)]
        message: Option<String>,
        /// Amend previous commit
        #[arg(long)]
        amend: bool,
    },
    /// Push changes across repos
    Push {
        /// Set upstream
        #[arg(short = 'u', long)]
        set_upstream: bool,
        /// Force push
        #[arg(short, long)]
        force: bool,
    },
    /// Clean up merged branches across repos
    Prune {
        /// Actually delete branches (default: dry-run)
        #[arg(long)]
        execute: bool,
        /// Also prune remote tracking refs
        #[arg(long)]
        remote: bool,
        /// Only prune repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
    /// Pull request operations
    Pr {
        #[command(subcommand)]
        action: PrCommands,
    },
    /// Griptree (worktree) operations
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
    },
    /// Pull latest changes across repos
    Pull {
        /// Rebase instead of merge
        #[arg(long)]
        rebase: bool,
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
    Bench(gitgrip::cli::commands::bench::BenchArgs),
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
        /// Only verify repos in these groups
        #[arg(long, value_delimiter = ',')]
        group: Option<Vec<String>>,
    },
}

#[derive(Subcommand)]
enum AgentCommands {
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
enum McpCommands {
    /// Start stdio MCP server exposing gitgrip tools
    Server,
}

#[derive(Subcommand)]
enum PrCommands {
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
    },
    /// Check CI status
    Checks,
    /// Show PR diff
    Diff {
        /// Show stat summary only
        #[arg(long)]
        stat: bool,
    },
}

#[derive(Subcommand)]
enum TreeCommands {
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
enum GroupCommands {
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
enum TargetCommands {
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
enum CiCommands {
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
enum ManifestCommands {
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
enum RepoCommands {
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

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Initialize tracing — `--verbose` enables debug logging for gitgrip
    if cli.verbose {
        tracing_subscriber::fmt()
            .with_env_filter("gitgrip=debug")
            .with_target(false)
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
            .init();
    }

    // Extract Copy flags before match moves cli.command
    let cli_quiet = cli.quiet;
    let cli_verbose = cli.verbose;
    let cli_json = cli.json;

    match cli.command {
        Some(Commands::Status { verbose, group }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::status::run_status(
                &ctx.workspace_root,
                &ctx.manifest,
                verbose,
                ctx.quiet,
                group.as_deref(),
                ctx.json,
            )?;
        }
        Some(Commands::Sync {
            force,
            reset_refs,
            group,
            sequential,
            no_hooks,
            rollback,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            if rollback {
                gitgrip::cli::commands::sync::run_sync_rollback(
                    &ctx.workspace_root,
                    &ctx.manifest,
                    ctx.quiet,
                    ctx.json,
                )
                .await?;
            } else {
                gitgrip::cli::commands::sync::run_sync(
                    &ctx.workspace_root,
                    &ctx.manifest,
                    force,
                    ctx.quiet,
                    group.as_deref(),
                    sequential,
                    reset_refs,
                    ctx.json,
                    no_hooks,
                )
                .await?;
            }
        }
        Some(Commands::Branch {
            name,
            delete,
            r#move,
            repo,
            include_manifest: _,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::branch::run_branch(
                gitgrip::cli::commands::branch::BranchOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    name: name.as_deref(),
                    delete,
                    move_commits: r#move,
                    repos_filter: repo.as_deref(),
                    group_filter: group.as_deref(),
                    json: ctx.json,
                },
            )?;
        }
        Some(Commands::Checkout { name, create, base }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            let branch = if base {
                let config = gitgrip::core::griptree::GriptreeConfig::load_from_workspace(
                    &ctx.workspace_root,
                )?
                .ok_or_else(|| anyhow::anyhow!("Not in a griptree workspace"))?;
                config.branch
            } else {
                name.ok_or_else(|| anyhow::anyhow!("Branch name is required"))?
            };

            gitgrip::cli::commands::checkout::run_checkout(
                &ctx.workspace_root,
                &ctx.manifest,
                &branch,
                create,
            )?;
        }
        Some(Commands::Add { files }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::add::run_add(&ctx.workspace_root, &ctx.manifest, &files)?;
        }
        Some(Commands::Diff { staged }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::diff::run_diff(
                &ctx.workspace_root,
                &ctx.manifest,
                staged,
                ctx.json,
            )?;
        }
        Some(Commands::Commit { message, amend }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            let msg = message.unwrap_or_else(|| {
                eprintln!("Error: commit message required (-m)");
                std::process::exit(1);
            });
            gitgrip::cli::commands::commit::run_commit(
                &ctx.workspace_root,
                &ctx.manifest,
                &msg,
                amend,
                ctx.json,
            )?;
        }
        Some(Commands::Push {
            set_upstream,
            force,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::push::run_push(
                &ctx.workspace_root,
                &ctx.manifest,
                set_upstream,
                force,
                ctx.quiet,
                ctx.json,
            )?;
        }
        Some(Commands::Prune {
            execute,
            remote,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::prune::run_prune(
                &ctx.workspace_root,
                &ctx.manifest,
                execute,
                remote,
                group.as_deref(),
            )?;
        }
        Some(Commands::Pr { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                PrCommands::Create {
                    title,
                    body,
                    push,
                    draft,
                    dry_run,
                } => {
                    gitgrip::cli::commands::pr::run_pr_create(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        title.as_deref(),
                        body.as_deref(),
                        draft,
                        push,
                        dry_run,
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Status => {
                    gitgrip::cli::commands::pr::run_pr_status(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Merge {
                    method,
                    force,
                    update,
                    auto,
                    wait,
                    timeout,
                } => {
                    gitgrip::cli::commands::pr::run_pr_merge(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        method.as_ref(),
                        force,
                        update,
                        auto,
                        ctx.json,
                        wait,
                        timeout,
                    )
                    .await?;
                }
                PrCommands::Checks => {
                    gitgrip::cli::commands::pr::run_pr_checks(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Diff { stat } => {
                    gitgrip::cli::commands::pr::run_pr_diff(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        stat,
                    )
                    .await?;
                }
            }
        }
        Some(Commands::Init {
            url,
            path,
            from_dirs,
            dirs,
            interactive,
            create_manifest,
            manifest_name,
            private,
            from_repo,
        }) => {
            gitgrip::cli::commands::init::run_init(gitgrip::cli::commands::init::InitOptions {
                url: url.as_deref(),
                path: path.as_deref(),
                from_dirs,
                dirs: &dirs,
                interactive,
                create_manifest,
                manifest_name: manifest_name.as_deref(),
                private,
                from_repo,
            })
            .await?;
        }
        Some(Commands::Tree { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                TreeCommands::Add { branch } => {
                    gitgrip::cli::commands::tree::run_tree_add(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &branch,
                    )?;
                }
                TreeCommands::List => {
                    gitgrip::cli::commands::tree::run_tree_list(&ctx.workspace_root)?;
                }
                TreeCommands::Remove { branch, force } => {
                    gitgrip::cli::commands::tree::run_tree_remove(
                        &ctx.workspace_root,
                        &branch,
                        force,
                    )?;
                }
                TreeCommands::Lock { branch, reason } => {
                    gitgrip::cli::commands::tree::run_tree_lock(
                        &ctx.workspace_root,
                        &branch,
                        reason.as_deref(),
                    )?;
                }
                TreeCommands::Unlock { branch } => {
                    gitgrip::cli::commands::tree::run_tree_unlock(&ctx.workspace_root, &branch)?;
                }
                TreeCommands::Return {
                    base,
                    no_sync,
                    autostash,
                    prune,
                    prune_current,
                    prune_remote,
                    force,
                } => {
                    gitgrip::cli::commands::tree::run_tree_return(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        base.as_deref(),
                        no_sync,
                        autostash,
                        prune.as_deref(),
                        prune_current,
                        prune_remote,
                        force,
                    )
                    .await?;
                }
            }
        }
        Some(Commands::Grep {
            pattern,
            ignore_case,
            parallel,
            pathspec,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::grep::run_grep(
                &ctx.workspace_root,
                &ctx.manifest,
                &pattern,
                ignore_case,
                parallel,
                &pathspec,
                group.as_deref(),
            )?;
        }
        Some(Commands::Forall {
            command,
            parallel,
            all,
            no_intercept,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::forall::run_forall(
                &ctx.workspace_root,
                &ctx.manifest,
                &command,
                parallel,
                !all, // Default: only repos with changes (changed_only=true unless --all)
                no_intercept,
                group.as_deref(),
            )?;
        }
        Some(Commands::Rebase {
            onto,
            upstream,
            abort,
            continue_rebase,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::rebase::run_rebase(
                &ctx.workspace_root,
                &ctx.manifest,
                onto.as_deref(),
                upstream,
                abort,
                continue_rebase,
            )?;
        }
        Some(Commands::Pull {
            rebase,
            group,
            sequential,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::pull::run_pull(
                &ctx.workspace_root,
                &ctx.manifest,
                rebase,
                group.as_deref(),
                sequential,
                ctx.quiet,
            )
            .await?;
        }
        Some(Commands::Link { status, apply }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::link::run_link(
                &ctx.workspace_root,
                &ctx.manifest,
                status,
                apply,
                ctx.json,
            )?;
        }
        Some(Commands::Run { name, list }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::run::run_run(
                &ctx.workspace_root,
                &ctx.manifest,
                name.as_deref(),
                list,
            )?;
        }
        Some(Commands::Env) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::env::run_env(&ctx.workspace_root, &ctx.manifest)?;
        }
        Some(Commands::Repo { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                RepoCommands::List => {
                    gitgrip::cli::commands::repo::run_repo_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                    )?;
                }
                RepoCommands::Add {
                    url,
                    path,
                    branch,
                    target,
                } => {
                    gitgrip::cli::commands::repo::run_repo_add(
                        &ctx.workspace_root,
                        &url,
                        path.as_deref(),
                        branch.as_deref(),
                        target.as_deref(),
                    )?;
                }
                RepoCommands::Remove { name, delete } => {
                    gitgrip::cli::commands::repo::run_repo_remove(
                        &ctx.workspace_root,
                        &name,
                        delete,
                    )?;
                }
            }
        }
        Some(Commands::Group { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                GroupCommands::List => {
                    gitgrip::cli::commands::group::run_group_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                    )?;
                }
                GroupCommands::Add { group, repos } => {
                    gitgrip::cli::commands::group::run_group_add(
                        &ctx.workspace_root,
                        &group,
                        &repos,
                    )?;
                }
                GroupCommands::Remove { group, repos } => {
                    gitgrip::cli::commands::group::run_group_remove(
                        &ctx.workspace_root,
                        &group,
                        &repos,
                    )?;
                }
                GroupCommands::Create { name } => {
                    gitgrip::cli::commands::group::run_group_create(&ctx.workspace_root, &name)?;
                }
            }
        }
        Some(Commands::Target { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                TargetCommands::List => {
                    gitgrip::cli::commands::target::run_target_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                    )?;
                }
                TargetCommands::Set { branch, repo } => {
                    if let Some(repo_name) = repo {
                        gitgrip::cli::commands::target::run_target_set_repo(
                            &ctx.workspace_root,
                            &repo_name,
                            &branch,
                        )?;
                    } else {
                        gitgrip::cli::commands::target::run_target_set(
                            &ctx.workspace_root,
                            &branch,
                        )?;
                    }
                }
                TargetCommands::Unset { repo } => {
                    if let Some(repo_name) = repo {
                        gitgrip::cli::commands::target::run_target_unset_repo(
                            &ctx.workspace_root,
                            &repo_name,
                        )?;
                    } else {
                        gitgrip::cli::commands::target::run_target_unset(
                            &ctx.workspace_root,
                        )?;
                    }
                }
            }
        }
        Some(Commands::Gc {
            aggressive,
            dry_run,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::gc::run_gc(
                &ctx.workspace_root,
                &ctx.manifest,
                aggressive,
                dry_run,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::CherryPick {
            commit,
            abort,
            continue_pick,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::cherry_pick::run_cherry_pick(
                &ctx.workspace_root,
                &ctx.manifest,
                commit.as_deref(),
                abort,
                continue_pick,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Ci { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                CiCommands::Run { name } => {
                    gitgrip::cli::commands::ci::run_ci_run(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &name,
                        ctx.json,
                    )?;
                }
                CiCommands::List => {
                    gitgrip::cli::commands::ci::run_ci_list(&ctx.manifest, ctx.json)?;
                }
                CiCommands::Status => {
                    gitgrip::cli::commands::ci::run_ci_status(&ctx.workspace_root, ctx.json)?;
                }
            }
        }
        Some(Commands::Manifest { action }) => match action {
            ManifestCommands::Import { path, output } => {
                gitgrip::cli::commands::manifest::run_manifest_import(&path, output.as_deref())?;
            }
            ManifestCommands::Sync => {
                let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
                gitgrip::cli::commands::manifest::run_manifest_sync(&ctx.workspace_root)?;
            }
            ManifestCommands::Schema { format } => {
                gitgrip::cli::commands::manifest::run_manifest_schema(&format)?;
            }
        },
        Some(Commands::Bench(args)) => {
            gitgrip::cli::commands::bench::run(args).await?;
        }
        Some(Commands::Agent { action }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            match action {
                AgentCommands::Context { repo } => {
                    gitgrip::cli::commands::agent::run_agent_context(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        ctx.json,
                    )?;
                }
                AgentCommands::Build { repo } => {
                    gitgrip::cli::commands::agent::run_agent_build(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::Test { repo } => {
                    gitgrip::cli::commands::agent::run_agent_test(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::Verify { repo } => {
                    gitgrip::cli::commands::agent::run_agent_verify(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::GenerateContext { dry_run } => {
                    gitgrip::cli::commands::agent::run_agent_generate_context(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        dry_run,
                        ctx.quiet,
                    )?;
                }
            }
        }
        Some(Commands::Mcp { action }) => match action {
            McpCommands::Server => {
                gitgrip::mcp::server::run_mcp_server()?;
            }
        },
        Some(Commands::Release {
            version,
            notes,
            dry_run,
            skip_pr,
            repo,
            timeout,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::release::run_release(
                gitgrip::cli::commands::release::ReleaseOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    version: &version,
                    notes: notes.as_deref(),
                    dry_run,
                    skip_pr,
                    target_repo: repo.as_deref(),
                    json: ctx.json,
                    quiet: ctx.quiet,
                    timeout,
                },
            )
            .await?;
        }
        Some(Commands::Completions { shell }) => {
            let mut cmd = Cli::command();
            generate(shell, &mut cmd, "gr", &mut std::io::stdout());
        }
        Some(Commands::Verify {
            clean,
            links,
            on_branch,
            synced,
            group,
        }) => {
            let ctx = load_workspace_context(cli_quiet, cli_verbose, cli_json)?;
            gitgrip::cli::commands::verify::run_verify(
                gitgrip::cli::commands::verify::VerifyOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    group_filter: group.as_deref(),
                    json: ctx.json,
                    quiet: ctx.quiet,
                    clean,
                    links,
                    on_branch: on_branch.as_deref(),
                    synced,
                },
            )?;
        }
        None => {
            let logo = r#"
                *    *
           *    |    |
           |    |    |
      *    |    |    |
      |    \    \    \
       \    \    \    \       *
        *    *    *    *       \
         \    \    \    \      *
          \    \    \    \    /
           *----*----*----*--*
            \        |      /
             \       |     /
              *------*----*
"#;
            let wordmark = r#"
        __ _ _ _              _
       / _` (_) |_ __ _ _ _ _(_)_ __
      | (_| | |  _/ _` | '_| | '_ \
       \__, |_|\__\__, |_| |_| .__/
       |___/      |___/      |_|
"#;
            print!("{}", logo.truecolor(251, 146, 60));
            print!("{}", wordmark.bold());
            println!("{}", "              git a grip.".truecolor(251, 146, 60));
            println!();
            println!("  Run {} for usage", "'gr --help'".dimmed());
        }
    }

    Ok(())
}

/// Load the gripspace manifest
fn load_gripspace() -> anyhow::Result<(std::path::PathBuf, gitgrip::core::manifest::Manifest)> {
    let current = std::env::current_dir()?;

    // First, check if we're in a griptree (has .griptree pointer file)
    if let Some((griptree_path, pointer)) =
        gitgrip::core::griptree::GriptreePointer::find_in_ancestors(&current)
    {
        return load_from_griptree(&griptree_path, &pointer);
    }

    // Not in a griptree - search parent directories for workspace root
    load_from_workspace(&current)
}

/// Load the gripspace manifest and return a WorkspaceContext with global CLI flags.
fn load_workspace_context(
    quiet: bool,
    verbose: bool,
    json: bool,
) -> anyhow::Result<gitgrip::cli::context::WorkspaceContext> {
    let (workspace_root, manifest) = load_gripspace()?;
    Ok(gitgrip::cli::context::WorkspaceContext::new(
        workspace_root,
        manifest,
        quiet,
        verbose,
        json,
    ))
}

/// Load manifest from a griptree workspace.
fn load_from_griptree(
    griptree_path: &std::path::Path,
    pointer: &gitgrip::core::griptree::GriptreePointer,
) -> anyhow::Result<(std::path::PathBuf, gitgrip::core::manifest::Manifest)> {
    let griptree_manifest_path =
        gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(griptree_path);

    let content = if let Some(path) = griptree_manifest_path {
        std::fs::read_to_string(path)?
    } else {
        let main_workspace = std::path::PathBuf::from(&pointer.main_workspace);
        let main_manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(&main_workspace);

        let main_path = main_manifest_path.ok_or_else(|| {
            anyhow::anyhow!(
                "Griptree points to main workspace '{}' but no gripspace manifest was found",
                pointer.main_workspace
            )
        })?;
        std::fs::read_to_string(main_path)?
    };

    let mut manifest = gitgrip::core::manifest::Manifest::parse(&content)?;
    resolve_gripspace_includes(&mut manifest, griptree_path);
    Ok((griptree_path.to_path_buf(), manifest))
}

/// Search parent directories for a workspace root and load its manifest.
fn load_from_workspace(
    start: &std::path::Path,
) -> anyhow::Result<(std::path::PathBuf, gitgrip::core::manifest::Manifest)> {
    let mut search_path = start.to_path_buf();
    loop {
        // Check for .gitgrip directory with gripspace manifest
        let gitgrip_dir = search_path.join(".gitgrip");
        if gitgrip_dir.exists() {
            if let Some(manifest_path) =
                gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(&search_path)
            {
                let content = std::fs::read_to_string(&manifest_path)?;
                let mut manifest = gitgrip::core::manifest::Manifest::parse(&content)?;
                resolve_gripspace_includes(&mut manifest, &search_path);
                return Ok((search_path, manifest));
            }
        }

        // Check for legacy repo.yaml
        if let Some(repo_yaml) =
            gitgrip::core::manifest_paths::resolve_repo_manifest_path(&search_path)
        {
            let content = std::fs::read_to_string(repo_yaml)?;
            let mut manifest = gitgrip::core::manifest::Manifest::parse(&content)?;
            resolve_gripspace_includes(&mut manifest, &search_path);
            return Ok((search_path, manifest));
        }

        // Fallback: parse .repo/manifest.xml directly (zero-config — just works)
        let repo_xml = search_path.join(".repo").join("manifest.xml");
        if repo_xml.exists() {
            let xml_manifest = gitgrip::core::repo_manifest::XmlManifest::parse_file(&repo_xml)?;
            let result = xml_manifest.to_manifest()?;
            return Ok((search_path, result.manifest));
        }

        match search_path.parent() {
            Some(parent) => search_path = parent.to_path_buf(),
            None => {
                anyhow::bail!("Not in a gitgrip workspace (no .gitgrip or .repo directory found)");
            }
        }
    }
}

/// Resolve gripspace includes (merge inherited repos/scripts/env/hooks) if spaces dir exists.
fn resolve_gripspace_includes(
    manifest: &mut gitgrip::core::manifest::Manifest,
    workspace_root: &std::path::Path,
) {
    let spaces_dir = gitgrip::core::manifest_paths::spaces_dir(workspace_root);
    if spaces_dir.exists() {
        let _ = gitgrip::core::gripspace::resolve_all_gripspaces(manifest, &spaces_dir);
    }
}
