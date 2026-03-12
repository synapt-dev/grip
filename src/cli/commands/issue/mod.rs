//! Issue command implementations
//!
//! Subcommands for issue operations (per-repo targeting).

mod close;
mod create;
mod list;
mod reopen;
mod view;

pub use close::run_issue_close;
pub use create::{run_issue_create, IssueCreateCommandOptions};
pub use list::{run_issue_list, IssueListOptions};
pub use reopen::run_issue_reopen;
pub use view::run_issue_view;

use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::platform::get_platform_adapter;
use std::path::Path;
use std::sync::Arc;

/// Resolve a single target repo from the manifest.
///
/// Issue commands target a single repo (unlike PR commands which fan out).
/// When `--repo` is provided, use that. Otherwise, auto-select if the
/// workspace has exactly one non-reference repo with a remote.
pub(crate) fn resolve_target_repo(
    workspace_root: &Path,
    manifest: &Manifest,
    repo_filter: Option<&str>,
) -> anyhow::Result<RepoInfo> {
    let repos: Vec<RepoInfo> = manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(
                name,
                config,
                workspace_root,
                &manifest.settings,
                manifest.remotes.as_ref(),
            )
        })
        .filter(|r| !r.reference && !r.url.is_empty())
        .collect();

    if let Some(name) = repo_filter {
        repos
            .into_iter()
            .find(|r| r.name == name)
            .ok_or_else(|| anyhow::anyhow!("Repository '{}' not found in manifest", name))
    } else if repos.len() == 1 {
        Ok(repos.into_iter().next().unwrap())
    } else if repos.is_empty() {
        anyhow::bail!("No repositories with remotes found in manifest")
    } else {
        let names: Vec<&str> = repos.iter().map(|r| r.name.as_str()).collect();
        anyhow::bail!(
            "Multiple repositories found. Use --repo to specify one of: {}",
            names.join(", ")
        )
    }
}

/// Get the platform adapter for a resolved repo.
pub(crate) fn get_adapter(
    repo: &RepoInfo,
) -> Arc<dyn crate::platform::HostingPlatform> {
    get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref())
}
