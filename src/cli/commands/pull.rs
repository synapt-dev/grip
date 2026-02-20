//! Pull command implementation
//!
//! Pulls latest changes across repositories (merge or rebase).

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::git::remote::{safe_pull_latest_with_mode, PullMode};
use crate::git::{open_repo, path_exists};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tokio::task::JoinSet;

/// Result of pulling a single repo
#[derive(Debug, Clone)]
struct PullResult {
    name: String,
    success: bool,
    message: String,
}

/// Run the pull command
pub async fn run_pull(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    rebase: bool,
    group_filter: Option<&[String]>,
    sequential: bool,
    quiet: bool,
) -> anyhow::Result<()> {
    let mut repos: Vec<RepoInfo> = filter_repos(manifest, workspace_root, None, group_filter, true);

    // Include manifest repo at the beginning (pull it first)
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        repos.insert(0, manifest_repo);
    }

    let mode = if rebase {
        PullMode::Rebase
    } else {
        PullMode::Merge
    };
    let mode_label = if rebase { "rebase" } else { "merge" };

    Output::header(&format!(
        "Pulling {} repositories ({})...",
        repos.len(),
        mode_label
    ));
    println!();

    let results = if sequential {
        pull_sequential(&repos, mode, quiet)?
    } else {
        pull_parallel(&repos, mode, quiet).await?
    };

    // Display results
    let mut success_count = 0;
    let mut error_count = 0;
    let mut failed_repos: Vec<(String, String)> = Vec::new();

    for result in results {
        if result.success {
            success_count += 1;
        } else {
            error_count += 1;
            failed_repos.push((result.name.clone(), result.message.clone()));
        }
    }

    println!();
    if error_count == 0 {
        Output::success(&format!(
            "All {} repositories pulled successfully.",
            success_count
        ));
    } else {
        Output::warning(&format!("{} pulled, {} failed", success_count, error_count));

        if !failed_repos.is_empty() {
            println!();
            for (repo_name, error_msg) in &failed_repos {
                println!("  ✗ {}: {}", repo_name, error_msg);
            }
        }
    }

    Ok(())
}

/// Pull repos sequentially
fn pull_sequential(
    repos: &[RepoInfo],
    mode: PullMode,
    quiet: bool,
) -> anyhow::Result<Vec<PullResult>> {
    let mut results = Vec::new();

    for repo in repos {
        let result = pull_single_repo(repo, mode, quiet, true)?;
        results.push(result);
    }

    Ok(results)
}

/// Pull repos in parallel using tokio
#[allow(clippy::unnecessary_to_owned)]
async fn pull_parallel(
    repos: &[RepoInfo],
    mode: PullMode,
    quiet: bool,
) -> anyhow::Result<Vec<PullResult>> {
    let results: Arc<Mutex<Vec<PullResult>>> = Arc::new(Mutex::new(Vec::new()));
    let mut join_set: JoinSet<anyhow::Result<()>> = JoinSet::new();

    let spinner = Output::spinner(&format!("Pulling {} repos in parallel...", repos.len()));

    for repo in repos.to_vec() {
        let results = Arc::clone(&results);

        join_set.spawn_blocking(move || {
            let result = pull_single_repo(&repo, mode, quiet, false)?;
            results.lock().expect("mutex poisoned").push(result);
            Ok(())
        });
    }

    while let Some(res) = join_set.join_next().await {
        res??;
    }

    spinner.finish_and_clear();

    let results = match Arc::try_unwrap(results) {
        Ok(mutex) => mutex.into_inner().expect("mutex poisoned"),
        Err(arc) => arc.lock().expect("mutex poisoned").clone(),
    };

    for result in &results {
        if result.success {
            if !quiet {
                Output::success(&format!("{}: {}", result.name, result.message));
            }
        } else {
            Output::error(&format!("{}: {}", result.name, result.message));
        }
    }

    Ok(results)
}

/// Pull a single repository
fn pull_single_repo(
    repo: &RepoInfo,
    mode: PullMode,
    quiet: bool,
    show_spinner: bool,
) -> anyhow::Result<PullResult> {
    let spinner = if show_spinner {
        Some(Output::spinner(&format!("Pulling {}...", repo.name)))
    } else {
        None
    };

    if !path_exists(&repo.absolute_path) {
        let message = "missing".to_string();
        if let Some(s) = spinner {
            s.finish_with_message(format!("{}: {}", repo.name, message));
        }
        return Ok(PullResult {
            name: repo.name.clone(),
            success: false,
            message,
        });
    }

    match open_repo(&repo.absolute_path) {
        Ok(git_repo) => {
            let result = safe_pull_latest_with_mode(
                &git_repo,
                repo.target_branch(),
                &repo.sync_remote,
                mode,
            );

            match result {
                Ok(pull_result) => {
                    let (success, message) = if pull_result.pulled {
                        if pull_result.recovered {
                            (
                                true,
                                pull_result
                                    .message
                                    .unwrap_or_else(|| "pulled (recovered)".to_string()),
                            )
                        } else {
                            (
                                true,
                                pull_result.message.unwrap_or_else(|| "pulled".to_string()),
                            )
                        }
                    } else if let Some(msg) = pull_result.message {
                        (true, msg)
                    } else {
                        (true, "up to date".to_string())
                    };

                    if let Some(s) = spinner {
                        if !quiet || !success {
                            s.finish_with_message(format!("{}: {}", repo.name, message));
                        } else {
                            s.finish_and_clear();
                        }
                    }

                    Ok(PullResult {
                        name: repo.name.clone(),
                        success,
                        message,
                    })
                }
                Err(e) => {
                    let message = format!("error - {}", e);
                    if let Some(s) = spinner {
                        s.finish_with_message(format!("{}: {}", repo.name, message));
                    }
                    Ok(PullResult {
                        name: repo.name.clone(),
                        success: false,
                        message,
                    })
                }
            }
        }
        Err(e) => {
            let message = format!("error - {}", e);
            if let Some(s) = spinner {
                s.finish_with_message(format!("{}: {}", repo.name, message));
            }
            Ok(PullResult {
                name: repo.name.clone(),
                success: false,
                message,
            })
        }
    }
}
