//! Workspace context passed to command handlers
//!
//! Bundles workspace state and global CLI flags into a single struct,
//! eliminating repetitive parameter passing across command handlers.

use crate::cli::output_sink::{OutputSink, TerminalSink};
use crate::core::manifest::Manifest;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Workspace context available to all command handlers.
///
/// Created once in `main()` after loading the gripspace, then passed
/// by reference to every command that needs workspace state.
pub struct WorkspaceContext {
    /// Root directory of the workspace
    pub workspace_root: PathBuf,
    /// Parsed workspace manifest
    pub manifest: Manifest,
    /// Suppress non-essential output (`--quiet`)
    pub quiet: bool,
    /// Show verbose output (`--verbose`)
    pub verbose: bool,
    /// Output in JSON format (`--json`)
    pub json: bool,
    /// Output sink for structured output (commands can adopt incrementally)
    pub sink: Arc<dyn OutputSink>,
}

impl WorkspaceContext {
    /// Get workspace root as a `&Path`
    pub fn root(&self) -> &Path {
        &self.workspace_root
    }

    /// Create a context with default [`TerminalSink`].
    pub fn new(
        workspace_root: PathBuf,
        manifest: Manifest,
        quiet: bool,
        verbose: bool,
        json: bool,
    ) -> Self {
        Self {
            workspace_root,
            manifest,
            quiet,
            verbose,
            json,
            sink: Arc::new(TerminalSink::new(quiet, json)),
        }
    }
}
