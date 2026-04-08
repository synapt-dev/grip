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

    /// Team workspace operations
    Team {
        #[command(subcommand)]
        command: TeamCommands,
    },

    /// Repo registry operations
    Repo {
        #[command(subcommand)]
        command: RepoCommands,
    },
}

#[derive(Subcommand, Debug)]
pub enum TeamCommands {
    /// Register an agent workspace under agents/
    Add {
        /// Agent workspace name
        name: String,
    },

    /// List registered agent workspaces
    List,

    /// Remove a registered agent workspace
    Remove {
        /// Agent workspace name
        name: String,
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
}
