//! Env command implementation
//!
//! Displays workspace environment variables.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use std::path::Path;

/// Run the env command
pub fn run_env(workspace_root: &Path, manifest: &Manifest) -> anyhow::Result<()> {
    Output::header("Workspace Environment");
    println!();

    // Built-in environment variables
    println!("  GITGRIP_WORKSPACE={}", workspace_root.display());
    let manifest_path = manifest_paths::resolve_gripspace_manifest_path(workspace_root)
        .or_else(|| manifest_paths::resolve_repo_manifest_path(workspace_root))
        .unwrap_or_else(|| manifest_paths::default_gripspace_manifest_path(workspace_root));
    println!("  GITGRIP_MANIFEST={}", manifest_path.display());

    // Workspace-defined environment variables
    if let Some(ref workspace) = manifest.workspace {
        if let Some(ref env_vars) = workspace.env {
            println!();
            println!("Workspace variables:");
            for (key, value) in env_vars {
                println!("  {}={}", key, value);
            }
        }
    }

    Ok(())
}
