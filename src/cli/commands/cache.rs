//! Cache command implementation
//!
//! Manages bare-repo caches under the machine-level cache root for workspace repos.

use crate::cli::args::CacheCommands;
use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;
use crate::core::workspace_cache;
use anyhow::Result;
use colored::Colorize;
use std::path::Path;

pub fn run_cache(
    workspace_root: &Path,
    manifest: &Manifest,
    action: CacheCommands,
    quiet: bool,
) -> Result<()> {
    // Include reference repos in caching — they benefit from local caches too
    let repos = filter_repos(manifest, workspace_root, None, None, true);

    match action {
        CacheCommands::Bootstrap => {
            if !quiet {
                Output::info(&format!(
                    "Bootstrapping caches for {} repos...",
                    repos.len()
                ));
            }

            let repo_pairs: Vec<(&str, &str)> = repos
                .iter()
                .map(|r| (r.name.as_str(), r.url.as_str()))
                .collect();

            let count = workspace_cache::bootstrap_all(workspace_root, repo_pairs.into_iter())?;

            if !quiet {
                if count > 0 {
                    Output::success(&format!("Bootstrapped {} new cache(s)", count));
                } else {
                    Output::info("All caches already exist");
                }
            }
        }

        CacheCommands::Update => {
            if !quiet {
                Output::info("Updating all caches...");
            }

            let repo_pairs: Vec<(&str, &str)> = repos
                .iter()
                .map(|r| (r.name.as_str(), r.url.as_str()))
                .collect();

            let count = workspace_cache::update_all(workspace_root, repo_pairs.into_iter())?;

            if !quiet {
                Output::success(&format!("Updated {} cache(s)", count));
            }
        }

        CacheCommands::Status => {
            println!(
                "{:<20} {:<8} {}",
                "Repo".bold(),
                "Status".bold(),
                "Path".bold()
            );
            println!("{}", "─".repeat(70));

            for repo in &repos {
                let exists = workspace_cache::cache_exists(workspace_root, &repo.name, &repo.url)?;
                let path =
                    workspace_cache::resolve_cache_path(workspace_root, &repo.name, &repo.url)?;
                let status = if exists {
                    "cached".green().to_string()
                } else {
                    "missing".yellow().to_string()
                };
                println!("{:<20} {:<8} {}", repo.name, status, path.display());
            }
        }

        CacheCommands::Remove { repo } => {
            let Some(repo_info) = repos.iter().find(|r| r.name == repo) else {
                anyhow::bail!("repo '{}' is not in this manifest", repo);
            };
            let removed =
                workspace_cache::remove_cache(workspace_root, &repo_info.name, &repo_info.url)?;
            if removed {
                Output::success(&format!("Removed cache for {}", repo));
            } else {
                Output::warning(&format!("No cache found for {}", repo));
            }
        }
    }

    Ok(())
}
