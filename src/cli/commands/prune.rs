//! Prune command implementation
//!
//! Deletes local branches that have been merged into the default branch.
//! Optionally prunes remote tracking refs.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::branch::{delete_local_branch, is_branch_merged, list_local_branches};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::util::log_cmd;
use std::path::Path;
use std::process::Command;

/// Run the prune command
pub fn run_prune(
    workspace_root: &Path,
    manifest: &Manifest,
    execute: bool,
    remote: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    if execute {
        Output::header("Pruning merged branches...");
    } else {
        Output::header("Pruning merged branches (dry run)...");
    }
    println!();

    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    let mut total_pruned = 0;
    let mut total_repos = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let git_repo = match open_repo(&repo.absolute_path) {
            Ok(r) => r,
            Err(e) => {
                Output::error(&format!("{}: {}", repo.name, e));
                continue;
            }
        };

        let current_branch = match get_current_branch(&git_repo) {
            Ok(b) => b,
            Err(e) => {
                Output::error(&format!("{}: {}", repo.name, e));
                continue;
            }
        };

        let branches = match list_local_branches(&git_repo) {
            Ok(b) => b,
            Err(e) => {
                Output::error(&format!("{}: {}", repo.name, e));
                continue;
            }
        };

        let mut merged_branches: Vec<String> = Vec::new();

        for branch in &branches {
            // Skip current branch and default branch
            if branch == &current_branch || branch == repo.target_branch() {
                continue;
            }

            match is_branch_merged(&git_repo, branch, repo.target_branch()) {
                Ok(true) => merged_branches.push(branch.clone()),
                Ok(false) => {}
                Err(e) => {
                    Output::warning(&format!(
                        "{}: could not check branch '{}': {}",
                        repo.name, branch, e
                    ));
                }
            }
        }

        if merged_branches.is_empty() {
            continue;
        }

        total_repos += 1;
        Output::subheader(&format!("{}:", repo.name));

        for branch in &merged_branches {
            if execute {
                match delete_local_branch(&git_repo, branch, false) {
                    Ok(()) => {
                        Output::success(&format!("  Deleted: {}", branch));
                        total_pruned += 1;
                    }
                    Err(e) => {
                        Output::error(&format!("  Failed to delete '{}': {}", branch, e));
                    }
                }
            } else {
                println!("  Would delete: {}", branch);
                total_pruned += 1;
            }
        }

        // Prune remote tracking refs if requested
        if remote {
            let mut cmd = Command::new("git");
            cmd.args(["fetch", "--prune"])
                .current_dir(&repo.absolute_path);
            log_cmd(&cmd);
            let output = cmd.output();

            match output {
                Ok(o) if o.status.success() => {
                    let stderr = String::from_utf8_lossy(&o.stderr);
                    if stderr.contains("pruning") || stderr.contains("[deleted]") {
                        Output::info("  Pruned remote tracking refs");
                    }
                }
                Ok(o) => {
                    let stderr = String::from_utf8_lossy(&o.stderr);
                    Output::warning(&format!("  Failed to prune remote refs: {}", stderr.trim()));
                }
                Err(e) => {
                    Output::warning(&format!("  Failed to prune remote refs: {}", e));
                }
            }
        }
    }

    println!();
    if total_pruned == 0 {
        Output::success("No merged branches to prune.");
    } else if execute {
        Output::success(&format!(
            "Pruned {} branch(es) across {} repo(s).",
            total_pruned, total_repos
        ));
    } else {
        Output::info(&format!(
            "Found {} merged branch(es) across {} repo(s).",
            total_pruned, total_repos
        ));
        Output::warning("Run with --execute to actually delete them.");
    }

    Ok(())
}
