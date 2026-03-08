//! Restore command implementation
//!
//! Unstages files or discards working-tree changes across repositories.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::cache::invalidate_status_cache;
use crate::git::{get_workdir, open_repo, path_exists};
use crate::util::log_cmd;
use git2::Repository;
use std::path::Path;
use std::process::Command;

/// Run the restore command
pub fn run_restore(
    workspace_root: &Path,
    manifest: &Manifest,
    files: &[String],
    staged: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let action = if staged { "unstage" } else { "discard" };
    Output::header(&format!(
        "Checking repositories for changes to {}...",
        action
    ));
    println!();

    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    let mut total_restored = 0;
    let mut repos_with_changes = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let count = restore_files(&git_repo, files, staged)?;
                if count > 0 {
                    let verb = if staged { "unstaged" } else { "discarded" };
                    Output::success(&format!("{}: {} {} file(s)", repo.name, verb, count));
                    total_restored += count;
                    repos_with_changes += 1;
                    invalidate_status_cache(&repo.absolute_path);
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    // Handle manifest worktree, respecting --repo filter
    let include_manifest = match repos_filter {
        None => true,
        Some(filter) => filter.iter().any(|r| r == "manifest"),
    };
    if include_manifest {
        if let Some(manifests_dir) = manifest_paths::resolve_manifest_repo_dir(workspace_root) {
            let manifests_git_dir = manifests_dir.join(".git");
            if manifests_git_dir.exists() && path_exists(&manifests_dir) {
                match open_repo(&manifests_dir) {
                    Ok(git_repo) => {
                        let count = restore_files(&git_repo, files, staged)?;
                        if count > 0 {
                            let verb = if staged { "unstaged" } else { "discarded" };
                            Output::success(&format!("manifest: {} {} file(s)", verb, count));
                            total_restored += count;
                            repos_with_changes += 1;
                            invalidate_status_cache(&manifests_dir);
                        }
                    }
                    Err(e) => Output::warning(&format!("manifest: {}", e)),
                }
            }
        }
    }

    println!();
    if total_restored > 0 {
        let verb = if staged { "Unstaged" } else { "Discarded" };
        println!(
            "{} {} file(s) in {} repository(s).",
            verb, total_restored, repos_with_changes
        );
    } else {
        let action_desc = if staged {
            "Nothing to unstage."
        } else {
            "Nothing to discard."
        };
        println!("{}", action_desc);
    }

    Ok(())
}

/// Restore files in a repository using git CLI
fn restore_files(repo: &Repository, files: &[String], staged: bool) -> anyhow::Result<usize> {
    let repo_dir = get_workdir(repo);

    // Count files that would be affected
    let count = if staged {
        count_staged_files(repo_dir)?
    } else {
        count_unstaged_changes(repo_dir)?
    };

    if count == 0 {
        return Ok(0);
    }

    // Build git restore command
    let mut args = vec!["restore"];
    if staged {
        args.push("--staged");
    }

    if files.len() == 1 && files[0] == "." {
        args.push(".");
    } else {
        for file in files {
            args.push(file);
        }
    }

    let mut cmd = Command::new("git");
    cmd.args(&args).current_dir(repo_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        // If there's nothing to restore, treat as 0 rather than error
        if stderr.contains("did not match any file") || stderr.contains("pathspec") {
            return Ok(0);
        }
        anyhow::bail!("git restore failed: {}", stderr);
    }

    // Count files actually affected (difference in state)
    let after_count = if staged {
        count_staged_files(repo_dir)?
    } else {
        count_unstaged_changes(repo_dir)?
    };

    Ok(count.saturating_sub(after_count))
}

/// Count staged files using git diff --cached
fn count_staged_files(repo_dir: &Path) -> anyhow::Result<usize> {
    let mut cmd = Command::new("git");
    cmd.args(["diff", "--cached", "--name-only"])
        .current_dir(repo_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;
    let count = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|l| !l.is_empty())
        .count();
    Ok(count)
}

/// Count unstaged working-tree changes using git diff
fn count_unstaged_changes(repo_dir: &Path) -> anyhow::Result<usize> {
    let mut cmd = Command::new("git");
    cmd.args(["diff", "--name-only"]).current_dir(repo_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;
    let count = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|l| !l.is_empty())
        .count();
    Ok(count)
}
