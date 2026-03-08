//! Rebase command implementation
//!
//! Rebases branches across repositories.

use crate::cli::output::Output;
use crate::core::griptree::GriptreeConfig;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::util::log_cmd;
use std::path::Path;
use std::process::Command;

/// Run the rebase command
#[allow(clippy::too_many_arguments)]
pub fn run_rebase(
    workspace_root: &Path,
    manifest: &Manifest,
    onto: Option<&str>,
    upstream: bool,
    abort: bool,
    _continue: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    if abort {
        return run_rebase_abort(workspace_root, manifest, repos_filter, group_filter);
    }

    if _continue {
        return run_rebase_continue(workspace_root, manifest, repos_filter, group_filter);
    }

    let use_upstream = upstream || onto.is_none();
    if upstream && onto.is_some() {
        Output::warning("Ignoring explicit target because --upstream was set");
    }
    if use_upstream {
        Output::header("Rebasing onto upstream branches");
    } else if let Some(target) = onto {
        Output::header(&format!("Rebasing onto {}", target));
    }
    println!();

    let griptree_config = GriptreeConfig::load_from_workspace(workspace_root)?;

    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    let mut success_count = 0;
    let mut skip_count = 0;
    let mut error_count = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            skip_count += 1;
            continue;
        }

        let git_repo = match open_repo(&repo.absolute_path) {
            Ok(r) => r,
            Err(_) => {
                skip_count += 1;
                continue;
            }
        };

        let branch = match get_current_branch(&git_repo) {
            Ok(b) => b,
            Err(_) => {
                skip_count += 1;
                continue;
            }
        };

        // Skip if on target branch
        if branch == repo.target_branch() {
            skip_count += 1;
            continue;
        }

        let spinner = Output::spinner(&format!("Rebasing {}...", repo.name));
        let target = if use_upstream {
            match griptree_config.as_ref() {
                Some(cfg) => match cfg.upstream_for_repo(&repo.name, &repo.revision) {
                    Ok(upstream) => upstream,
                    Err(e) => {
                        spinner.finish_with_message(format!("{}: error - {}", repo.name, e));
                        error_count += 1;
                        continue;
                    }
                },
                None => repo.sync_ref(),
            }
        } else {
            onto.unwrap_or("origin/main").to_string()
        };

        // Use git command for rebase (git2 doesn't support interactive rebase well)
        let mut cmd = Command::new("git");
        cmd.args(["rebase", &target])
            .current_dir(&repo.absolute_path);
        log_cmd(&cmd);
        let output = cmd.output()?;

        if output.status.success() {
            spinner.finish_with_message(format!("{}: rebased", repo.name));
            success_count += 1;
        } else {
            let stderr = String::from_utf8_lossy(&output.stderr);
            if stderr.contains("CONFLICT") {
                spinner.finish_with_message(format!(
                    "{}: conflicts - resolve and run 'gr rebase --continue'",
                    repo.name
                ));
            } else {
                spinner.finish_with_message(format!("{}: failed", repo.name));
            }
            error_count += 1;
        }
    }

    println!();
    if error_count == 0 {
        Output::success(&format!(
            "Rebased {} repo(s){}",
            success_count,
            if skip_count > 0 {
                format!(", {} skipped", skip_count)
            } else {
                String::new()
            }
        ));
    } else {
        Output::warning(&format!(
            "{} rebased, {} failed, {} skipped",
            success_count, error_count, skip_count
        ));
        println!();
        println!("To continue after resolving conflicts: gr rebase --continue");
        println!("To abort the rebase: gr rebase --abort");
    }

    Ok(())
}

fn run_rebase_abort(
    workspace_root: &Path,
    manifest: &Manifest,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    Output::header("Aborting rebase");
    println!();

    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        // Check if rebase is in progress
        let rebase_dir = repo.absolute_path.join(".git/rebase-merge");
        let rebase_apply_dir = repo.absolute_path.join(".git/rebase-apply");

        if rebase_dir.exists() || rebase_apply_dir.exists() {
            let mut cmd = Command::new("git");
            cmd.args(["rebase", "--abort"])
                .current_dir(&repo.absolute_path);
            log_cmd(&cmd);
            let output = cmd.output()?;

            if output.status.success() {
                Output::success(&format!("{}: rebase aborted", repo.name));
            } else {
                Output::error(&format!("{}: failed to abort", repo.name));
            }
        }
    }

    Ok(())
}

fn run_rebase_continue(
    workspace_root: &Path,
    manifest: &Manifest,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    Output::header("Continuing rebase");
    println!();

    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        // Check if rebase is in progress
        let rebase_dir = repo.absolute_path.join(".git/rebase-merge");
        let rebase_apply_dir = repo.absolute_path.join(".git/rebase-apply");

        if rebase_dir.exists() || rebase_apply_dir.exists() {
            let mut cmd = Command::new("git");
            cmd.args(["rebase", "--continue"])
                .current_dir(&repo.absolute_path);
            log_cmd(&cmd);
            let output = cmd.output()?;

            if output.status.success() {
                Output::success(&format!("{}: rebase continued", repo.name));
            } else {
                let stderr = String::from_utf8_lossy(&output.stderr);
                if stderr.contains("CONFLICT") {
                    Output::warning(&format!("{}: still has conflicts", repo.name));
                } else {
                    Output::error(&format!("{}: failed to continue", repo.name));
                }
            }
        }
    }

    Ok(())
}
