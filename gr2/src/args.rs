use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(
    name = "gr2",
    about = "Clean-break gitgrip CLI for clone-backed team workspaces",
    long_about = "gr2 is the clean-break gitgrip CLI for the new team-workspace, cache, and checkout model.",
    version,
    arg_required_else_help = true
)]
pub struct Cli {
    /// Enable verbose logging
    #[arg(short, long)]
    pub verbose: bool,

    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// Initialize a new team workspace root
    Init {
        /// Path to create the workspace in
        path: String,

        /// Optional logical workspace name
        #[arg(long)]
        name: Option<String>,
    },

    /// Verify the gr2 bootstrap binary is wired correctly
    Doctor,

    /// Repo registry operations
    Repo {
        #[command(subcommand)]
        command: RepoCommands,
    },

    /// Unit registry operations
    Unit {
        #[command(subcommand)]
        command: UnitCommands,
    },

    /// Lane metadata operations
    Lane {
        #[command(subcommand)]
        command: LaneCommands,
    },

    /// Lane-aware execution planning and commands
    Exec {
        #[command(subcommand)]
        command: ExecCommands,
    },

    /// Declarative workspace spec operations
    Spec {
        #[command(subcommand)]
        command: SpecCommands,
    },

    /// Diff the workspace spec into an execution plan
    Plan {
        /// Pre-approve plans with more than 3 operations
        #[arg(long)]
        yes: bool,
    },

    /// Apply the current execution plan to the workspace
    Apply {
        /// Pre-approve plans with more than 3 operations
        #[arg(long)]
        yes: bool,

        /// Automatically stash and restore uncommitted changes in dirty repos
        #[arg(long)]
        autostash: bool,
    },
}

#[derive(Subcommand, Debug)]
pub enum RepoCommands {
    /// Register a repo in the team workspace
    Add {
        /// Logical repo name
        name: String,

        /// Canonical remote URL
        url: String,
    },

    /// List registered repos
    List,

    /// Inspect repo-maintenance state across shared repos and unit repos
    Status {
        /// Only show repo state for a specific unit
        #[arg(long)]
        unit: Option<String>,

        /// Only show a specific repo name
        #[arg(long)]
        repo: Option<String>,
    },

    /// Remove a registered repo
    Remove {
        /// Logical repo name
        name: String,
    },
}

#[derive(Subcommand, Debug)]
pub enum UnitCommands {
    /// Register a local unit in the workspace materialization model
    Add {
        /// Unit name
        name: String,
    },

    /// List registered units
    List,

    /// Remove a registered unit
    Remove {
        /// Unit name
        name: String,
    },
}

#[derive(Subcommand, Debug)]
pub enum LaneCommands {
    /// Create a lane record and scaffold its workspace directories
    Create {
        /// Lane name
        name: String,

        /// Owning unit
        #[arg(long = "owner-unit")]
        owner_unit: String,

        /// Lane type
        #[arg(long = "type", default_value = "feature")]
        lane_type: String,

        /// Repo membership for the lane (defaults to unit repos from workspace spec)
        #[arg(long = "repo")]
        repos: Vec<String>,

        /// Branch intent entries in repo=branch form
        #[arg(long = "branch")]
        branches: Vec<String>,

        /// Extra shared context roots relative to workspace root
        #[arg(long = "shared-context")]
        shared_context: Vec<String>,

        /// Extra private context roots relative to workspace root
        #[arg(long = "private-context")]
        private_context: Vec<String>,

        /// Default execution command for the lane (repeatable)
        #[arg(long = "exec")]
        exec: Vec<String>,

        /// PR associations in repo:number form
        #[arg(long = "pr")]
        prs: Vec<String>,

        /// Creation source label
        #[arg(long)]
        source: Option<String>,

        /// Allow execution fan-out across repos by default
        #[arg(long)]
        parallel: bool,

        /// Do not fail fast when running multi-repo commands
        #[arg(long)]
        no_fail_fast: bool,
    },

    /// List persisted lanes
    List {
        /// Filter to one owner unit
        #[arg(long = "owner-unit")]
        owner_unit: Option<String>,
    },

    /// Print one lane record
    Show {
        /// Lane name
        name: String,

        /// Owning unit
        #[arg(long = "owner-unit")]
        owner_unit: String,
    },

    /// Remove one lane record and its scaffolded directories
    Remove {
        /// Lane name
        name: String,

        /// Owning unit
        #[arg(long = "owner-unit")]
        owner_unit: String,
    },
}

#[derive(Subcommand, Debug)]
pub enum ExecCommands {
    /// Show the execution plan surface for one lane
    Status {
        /// Lane name
        #[arg(long = "lane")]
        lane: String,

        /// Owning unit
        #[arg(long = "owner-unit")]
        owner_unit: String,

        /// Filter to one or more repos inside the lane
        #[arg(long = "repo")]
        repos: Vec<String>,
    },
}

#[derive(Subcommand, Debug)]
pub enum SpecCommands {
    /// Print the current workspace spec
    Show,

    /// Validate the current workspace spec against the filesystem
    Validate,
}
