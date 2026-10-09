//! gitgrip CLI entry point

use clap::Parser;
use gitgrip::cli::args::Cli;
use gitgrip::cli::outcome::{error_was_reported, exit_code_for_error, render_unreported_error};
use std::process::ExitCode;

fn main() -> ExitCode {
    // `gr` resolves gr1 or gr2 before anything else runs, and before the runtime starts threads.
    if let Some(code) = gitgrip::gr_resolver::entry() {
        return ExitCode::from(u8::try_from(code).unwrap_or(1));
    }
    gr1_main()
}

#[tokio::main]
async fn gr1_main() -> ExitCode {
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

    match gitgrip::cli::dispatch::dispatch_command(cli.command, quiet, verbose, json).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if !error_was_reported(&error) {
                eprintln!("{}", render_unreported_error(&error));
            }
            ExitCode::from(exit_code_for_error(&error))
        }
    }
}
