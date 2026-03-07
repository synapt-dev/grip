//! CLI layer
//!
//! Command-line interface using clap.

pub mod args;
pub mod commands;
pub mod context;
pub mod dispatch;
pub mod output;
pub mod output_sink;
pub mod repo_iter;

pub use args::Cli;
pub use context::WorkspaceContext;
pub use output::Output;
pub use output_sink::OutputSink;
