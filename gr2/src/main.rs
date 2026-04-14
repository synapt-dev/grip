//! gr2 CLI entry point (development binary, not shipped with `cargo install gitgrip`)

use clap::Parser;
use gr2_cli::args::Cli;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    if cli.verbose {
        tracing_subscriber::fmt()
            .with_env_filter("gr2=debug")
            .with_target(false)
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
            .init();
    }

    gr2_cli::dispatch::dispatch_command(cli.command, cli.verbose).await
}
