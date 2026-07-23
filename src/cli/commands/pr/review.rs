//! PR review command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{
    filter_repos, get_manifest_repo_info, require_explicit_multi_repo_scope,
    validate_repo_filters_known,
};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use crate::platform::ReviewEvent;
use std::path::Path;
use std::sync::Arc;

struct PRToReview {
    repo_name: String,
    owner: String,
    repo: String,
    pr_number: u64,
    platform: Arc<dyn crate::platform::HostingPlatform>,
}

/// Run the PR review command — post a review on PRs across linked repos
pub async fn run_pr_review(
    workspace_root: &Path,
    manifest: &Manifest,
    event: ReviewEvent,
    body: Option<&str>,
    repo_filter: Option<&[String]>,
    allow_all: bool,
    json: bool,
) -> anyhow::Result<()> {
    if matches!(event, ReviewEvent::RequestChanges | ReviewEvent::Comment) && body.is_none() {
        anyhow::bail!("--body is required for comment and request-changes reviews");
    }

    validate_repo_filters_known(manifest, repo_filter)?;

    if !json {
        let action = match event {
            ReviewEvent::Approve => "Approving",
            ReviewEvent::RequestChanges => "Requesting changes on",
            ReviewEvent::Comment => "Commenting on",
        };
        Output::header(&format!("{} pull requests...", action));
        println!();
    }

    let repos = filter_repos(manifest, workspace_root, repo_filter, None, false);

    let mut all_repos = repos;
    let manifest_included = match repo_filter {
        Some(filter) => filter.iter().any(|f| f == "manifest"),
        None => true,
    };
    if manifest_included {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            all_repos.push(manifest_repo);
        }
    }

    // First pass: find which repos actually have a matching open PR, without
    // reviewing anything yet -- same reasoning as run_pr_edit's find/act split.
    let mut prs_to_review: Vec<PRToReview> = Vec::new();
    let mut skipped = 0u32;
    let mut find_errors = Vec::new();

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
                prs_to_review.push(PRToReview {
                    repo_name: repo.name.clone(),
                    owner: repo.owner.clone(),
                    repo: repo.repo.clone(),
                    pr_number: pr.number,
                    platform,
                });
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
                find_errors.push(format!("{}: {}", repo.name, e));
            }
        }
    }

    require_explicit_multi_repo_scope(
        &prs_to_review,
        repo_filter.is_some(),
        allow_all,
        "gr pr review",
        |pr| {
            format!(
                "{} PR #{} on {}/{}",
                pr.repo_name, pr.pr_number, pr.owner, pr.repo
            )
        },
    )?;

    let mut reviewed = 0u32;
    let mut errors = find_errors;

    for pr in &prs_to_review {
        let spinner = Output::spinner(&format!(
            "Reviewing {} PR #{}...",
            pr.repo_name, pr.pr_number
        ));
        match pr
            .platform
            .create_pull_request_review(&pr.owner, &pr.repo, pr.pr_number, event, body)
            .await
        {
            Ok(()) => {
                spinner.finish_with_message(format!(
                    "{}: reviewed PR #{} on {}/{}",
                    pr.repo_name, pr.pr_number, pr.owner, pr.repo
                ));
                reviewed += 1;
            }
            Err(e) => {
                spinner.finish_with_message(format!(
                    "{}: failed to review PR #{}: {}",
                    pr.repo_name, pr.pr_number, e
                ));
                errors.push(format!("{}: {}", pr.repo_name, e));
            }
        }
    }

    if json {
        let result = serde_json::json!({
            "reviewed": reviewed,
            "skipped": skipped,
            "errors": errors,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if reviewed > 0 {
            Output::success(&format!("{} PR(s) reviewed", reviewed));
        } else if errors.is_empty() {
            Output::info("No open PRs found to review");
        }
    }

    if !errors.is_empty() {
        anyhow::bail!("{} error(s) occurred", errors.len());
    }

    Ok(())
}
