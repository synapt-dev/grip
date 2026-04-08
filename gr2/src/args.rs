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
    /// Verify the gr2 bootstrap binary is wired correctly
    Doctor,
}
