//! Checkout command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::core::workspace_checkout;
use crate::git::{
    branch::{branch_exists, checkout_branch, create_and_checkout_branch},
    open_repo,
};
use std::path::Path;

/// Run the checkout command
pub fn run_checkout(
    workspace_root: &Path,
    manifest: &Manifest,
    branch_name: &str,
    create: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let action = if create {
        "Creating and checking out"
    } else {
        "Checking out"
    };

    let mut repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    // Include manifest repo, respecting --repo filter
    let include_manifest = match repos_filter {
        None => true,
        Some(filter) => filter.iter().any(|r| r == "manifest"),
    };
    if include_manifest {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            repos.push(manifest_repo);
        }
    }

    Output::header(&format!(
        "{} '{}' in {} repos...",
        action,
        branch_name,
        repos.len()
    ));
    println!();

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

/// Materialize an independent child checkout from cached repos.
///
/// This reserves `gr checkout add <name>` while preserving the existing
/// `gr checkout <branch>` behavior for cross-repo branch switching.
pub fn run_checkout_add(
    workspace_root: &Path,
    manifest: &Manifest,
    checkout_name: &str,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let mut repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    let include_manifest = match repos_filter {
        None => true,
        Some(filter) => filter.iter().any(|r| r == "manifest"),
    };
    if include_manifest {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            repos.push(manifest_repo);
        }
    }

    if repos.is_empty() {
        anyhow::bail!("no repos matched checkout filters");
    }

    let repo_specs: Vec<(&str, &str, &str)> = repos
        .iter()
        .map(|repo| (repo.name.as_str(), repo.url.as_str(), repo.path.as_str()))
        .collect();

    let info = workspace_checkout::create_checkout(
        workspace_root,
        checkout_name,
        repo_specs.into_iter(),
        None,
    )?;

    Output::success(&format!(
        "Created checkout '{}' with {} repo(s)",
        info.name,
        info.repos.len()
    ));
    Output::info(&format!("Path: {}", info.path.display()));
    Ok(())
}

/// List cache-backed child checkouts.
pub fn run_checkout_list(workspace_root: &Path) -> anyhow::Result<()> {
    Output::header("Checkouts");
    println!();

    let checkouts = workspace_checkout::list_checkouts(workspace_root)?;
    if checkouts.is_empty() {
        println!("No checkouts configured.");
        return Ok(());
    }

    for checkout in checkouts {
        println!("{} -> {}", checkout.name, checkout.path.display());
    }

    Ok(())
}

/// Remove a cache-backed child checkout.
pub fn run_checkout_remove(workspace_root: &Path, checkout_name: &str) -> anyhow::Result<()> {
    Output::header(&format!("Removing checkout '{}'", checkout_name));
    println!();

    let removed = workspace_checkout::remove_checkout(workspace_root, checkout_name)?;
    if removed {
        Output::success(&format!("Removed checkout '{}'", checkout_name));
        Ok(())
    } else {
        anyhow::bail!("Checkout '{}' not found", checkout_name);
    }
}
