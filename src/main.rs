//! gitgrip CLI entry point

use clap::Parser;
use gitgrip::cli::args::Cli;

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

    let quiet = cli.quiet;
    let verbose = cli.verbose;
    let json = cli.json;

    gitgrip::cli::dispatch::dispatch_command(cli.command, quiet, verbose, json).await
}
