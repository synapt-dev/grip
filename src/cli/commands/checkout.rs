//! Checkout command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{get_manifest_repo_info, RepoInfo};
use crate::git::{
    branch::{branch_exists, checkout_branch, create_and_checkout_branch},
    open_repo,
};
use std::path::PathBuf;

/// Run the checkout command
pub fn run_checkout(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    branch_name: &str,
    create: bool,
) -> anyhow::Result<()> {
    let action = if create {
        "Creating and checking out"
    } else {
        "Checking out"
    };
    Output::header(&format!(
        "{} '{}' in {} repos...",
        action,
        branch_name,
        manifest.repos.len()
    ));
    println!();

    let mut repos: Vec<RepoInfo> = manifest
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
        .filter(|r| !r.reference) // Skip reference repos
        .collect();

    // Include manifest repo in checkout operations
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        repos.push(manifest_repo);
    }

    let mut success_count = 0;
    let mut _skip_count = 0;

    for repo in &repos {
        if !repo.exists() {
            Output::warning(&format!("{}: not cloned", repo.name));
            _skip_count += 1;
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let exists = branch_exists(&git_repo, branch_name);

                if create {
                    // -b flag: create if doesn't exist, checkout if it does
                    if exists {
                        match checkout_branch(&git_repo, branch_name) {
                            Ok(()) => {
                                Output::success(&format!(
                                    "{}: checked out (already exists)",
                                    repo.name
                                ));
                                success_count += 1;
                            }
                            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
                        }
                    } else {
                        match create_and_checkout_branch(&git_repo, branch_name) {
                            Ok(()) => {
                                Output::success(&format!("{}: created and checked out", repo.name));
                                success_count += 1;
                            }
                            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
                        }
                    }
                } else {
                    // Normal checkout: skip if branch doesn't exist
                    if !exists {
                        Output::info(&format!("{}: branch doesn't exist, skipping", repo.name));
                        _skip_count += 1;
                        continue;
                    }

                    match checkout_branch(&git_repo, branch_name) {
                        Ok(()) => {
                            Output::success(&repo.name);
                            success_count += 1;
                        }
                        Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
                    }
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    println!();
    println!(
        "Switched {}/{} repos to {}",
        success_count,
        repos.len(),
        Output::branch_name(branch_name)
    );

    Ok(())
}
