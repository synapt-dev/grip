//! PR edit command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{get_manifest_repo_info, RepoInfo};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use std::path::Path;

/// Run the PR edit command — update title and/or body across linked PRs
pub async fn run_pr_edit(
    workspace_root: &Path,
    manifest: &Manifest,
    title: Option<&str>,
    body: Option<&str>,
    json: bool,
) -> anyhow::Result<()> {
    if title.is_none() && body.is_none() {
        anyhow::bail!("At least one of --title or --body must be provided");
    }

    if !json {
        Output::header("Editing pull requests...");
        println!();
    }

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
        .filter(|r| !r.reference)
        .collect();

    let mut all_repos = repos;
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        all_repos.push(manifest_repo);
    }

    let mut updated = 0u32;
    let mut skipped = 0u32;
    let mut errors = Vec::new();

    for repo in &all_repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let git_repo = match open_repo(&repo.absolute_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        let branch = match get_current_branch(&git_repo) {
            Ok(b) => b,
            Err(_) => continue,
        };

        // Skip if on target branch (no PR expected)
        if branch == repo.target_branch() {
            continue;
        }

        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        match platform
            .find_pr_by_branch(&repo.owner, &repo.repo, &branch)
            .await
        {
            Ok(Some(pr)) => {
                match platform
                    .update_pull_request(&repo.owner, &repo.repo, pr.number, title, body)
                    .await
                {
                    Ok(()) => {
                        if !json {
                            Output::success(&format!(
                                "{}: updated PR #{} on {}/{}",
                                repo.name, pr.number, repo.owner, repo.repo
                            ));
                        }
                        updated += 1;
                    }
                    Err(e) => {
                        if !json {
                            Output::error(&format!(
                                "{}: failed to update PR #{}: {}",
                                repo.name, pr.number, e
                            ));
                        }
                        errors.push(format!("{}: {}", repo.name, e));
                    }
                }
            }
            Ok(None) => {
                if !json {
                    Output::info(&format!(
                        "{}: no open PR for branch '{}'",
                        repo.name, branch
                    ));
                }
                skipped += 1;
            }
            Err(e) => {
                if !json {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
                errors.push(format!("{}: {}", repo.name, e));
            }
        }
    }

    if json {
        let result = serde_json::json!({
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if updated > 0 {
            Output::success(&format!("{} PR(s) updated", updated));
        } else if errors.is_empty() {
            Output::info("No open PRs found to edit");
        }
    }

    if !errors.is_empty() {
        anyhow::bail!("{} error(s) occurred", errors.len());
    }

    Ok(())
}
