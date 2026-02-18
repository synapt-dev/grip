//! PR merge command implementation

use super::create::has_commits_ahead;
use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{get_manifest_repo_info, RepoInfo};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::traits::PlatformError;
use crate::platform::{get_platform_adapter, CheckState};
use std::path::PathBuf;
use std::sync::Arc;

/// Run the PR merge command
#[allow(clippy::too_many_arguments)]
pub async fn run_pr_merge(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    method: Option<&crate::platform::MergeMethod>,
    force: bool,
    update: bool,
    auto: bool,
    json: bool,
    wait: bool,
    timeout: u64,
) -> anyhow::Result<()> {
    if !json {
        Output::header("Merging pull requests...");
        println!();
    }

    let repos: Vec<RepoInfo> = manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(name, config, workspace_root, &manifest.settings)
        })
        .filter(|r| !r.reference) // Skip reference repos
        .collect();

    let merge_method = method.copied().unwrap_or_default();

    // Also check manifest repo if configured
    let mut all_repos = repos.clone();
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        // Only add manifest repo if it has changes
        match check_repo_for_changes(&manifest_repo) {
            Ok(true) => {
                all_repos.push(manifest_repo);
            }
            Ok(false) => {
                Output::info("manifest: no changes, skipping");
            }
            Err(e) => {
                Output::warning(&format!("manifest: could not check for changes: {}", e));
            }
        }
    }

    // Collect PRs to merge
    #[derive(Debug, Clone, Copy)]
    enum CheckStatus {
        Passing,
        Failing,
        Pending,
        Unknown,
    }

    struct PRToMerge {
        repo_name: String,
        owner: String,
        repo: String,
        branch: String,
        pr_number: u64,
        platform: Arc<dyn crate::platform::HostingPlatform>,
        approved: bool,
        check_status: CheckStatus,
        mergeable: bool,
    }

    let mut prs_to_merge: Vec<PRToMerge> = Vec::new();
    let mut json_skipped: Vec<String> = Vec::new();

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

        // Skip if on target branch
        if branch == repo.target_branch() {
            continue;
        }

        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        match platform
            .find_pr_by_branch(&repo.owner, &repo.repo, &branch)
            .await
        {
            Ok(Some(pr)) => {
                // Get PR details
                let (approved, mergeable) = match platform
                    .get_pull_request(&repo.owner, &repo.repo, pr.number)
                    .await
                {
                    Ok(full_pr) => {
                        let is_approved = platform
                            .is_pull_request_approved(&repo.owner, &repo.repo, pr.number)
                            .await
                            .unwrap_or(false);
                        (is_approved, full_pr.mergeable.unwrap_or(false))
                    }
                    Err(_) => (false, false),
                };

                // Get status checks
                let check_status = match platform
                    .get_status_checks(&repo.owner, &repo.repo, &branch)
                    .await
                {
                    Ok(status) => {
                        // Successfully got check status
                        if status.state == CheckState::Failure {
                            // Checks are actually failing
                            CheckStatus::Failing
                        } else if status.state == CheckState::Pending {
                            // Checks still running - don't block but warn
                            CheckStatus::Pending
                        } else {
                            CheckStatus::Passing
                        }
                    }
                    Err(e) => {
                        // Could not determine check status
                        // Don't block merge due to API issues
                        Output::warning(&format!(
                            "{}: Could not check CI status for PR #{}: {}",
                            repo.name, pr.number, e
                        ));
                        CheckStatus::Unknown
                    }
                };

                prs_to_merge.push(PRToMerge {
                    repo_name: repo.name.clone(),
                    owner: repo.owner.clone(),
                    repo: repo.repo.clone(),
                    branch: branch.clone(),
                    pr_number: pr.number,
                    platform,
                    approved,
                    check_status,
                    mergeable,
                });
            }
            Ok(None) => {
                if !json {
                    Output::info(&format!("{}: no open PR for this branch", repo.name));
                }
                json_skipped.push(repo.name.clone());
            }
            Err(e) => {
                if !json {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
            }
        }
    }

    if prs_to_merge.is_empty() {
        println!("No open PRs found for any repository.");
        println!("Repositories checked: {}", all_repos.len());
        return Ok(());
    }

    // Show which repos have PRs and which don't
    let repos_with_prs: Vec<String> = prs_to_merge.iter().map(|p| p.repo_name.clone()).collect();
    let repos_without_prs: Vec<String> = all_repos
        .iter()
        .filter(|r| !repos_with_prs.contains(&r.name))
        .map(|r| r.name.clone())
        .collect();

    if !repos_without_prs.is_empty() {
        Output::info(&format!(
            "Merging {} repo(s) with open PRs. {} repo(s) have no open PRs and will be skipped.",
            prs_to_merge.len(),
            repos_without_prs.len()
        ));
        for repo_name in &repos_without_prs {
            Output::info(&format!("  - {}: skipped (no open PR)", repo_name));
        }
        println!();
    }

    // Wait for checks to pass if --wait
    if wait {
        let any_pending = prs_to_merge
            .iter()
            .any(|pr| matches!(pr.check_status, CheckStatus::Pending));

        if any_pending {
            let start = std::time::Instant::now();
            let timeout_duration = std::time::Duration::from_secs(timeout);

            let spinner = Output::spinner("Waiting for checks to pass...");

            loop {
                let pending_count = prs_to_merge
                    .iter()
                    .filter(|pr| matches!(pr.check_status, CheckStatus::Pending))
                    .count();

                if pending_count == 0 {
                    break;
                }

                if start.elapsed() > timeout_duration {
                    spinner.finish_with_message("Timed out waiting for checks");
                    anyhow::bail!(
                        "Timed out after {} seconds waiting for checks to pass",
                        timeout
                    );
                }

                // Early exit if all remaining non-passing checks have definitively failed
                let all_resolved = prs_to_merge
                    .iter()
                    .all(|pr| !matches!(pr.check_status, CheckStatus::Pending));
                if all_resolved {
                    break;
                }

                let elapsed = start.elapsed().as_secs();
                spinner.set_message(format!(
                    "Waiting for checks... ({} pending, {}s elapsed)",
                    pending_count, elapsed
                ));

                tokio::time::sleep(std::time::Duration::from_secs(15)).await;

                // Re-poll check status for pending PRs
                for pr in &mut prs_to_merge {
                    if !matches!(pr.check_status, CheckStatus::Pending) {
                        continue;
                    }

                    match pr
                        .platform
                        .get_status_checks(&pr.owner, &pr.repo, &pr.branch)
                        .await
                    {
                        Ok(status) => {
                            pr.check_status = match status.state {
                                CheckState::Failure => CheckStatus::Failing,
                                CheckState::Pending => CheckStatus::Pending,
                                _ => CheckStatus::Passing,
                            };

                            match pr.check_status {
                                CheckStatus::Passing => {
                                    Output::success(&format!(
                                        "{} PR #{}: checks passed",
                                        pr.repo_name, pr.pr_number
                                    ));
                                }
                                CheckStatus::Failing => {
                                    Output::error(&format!(
                                        "{} PR #{}: checks failed",
                                        pr.repo_name, pr.pr_number
                                    ));
                                }
                                _ => {}
                            }
                        }
                        Err(_) => {
                            // Keep as pending, will retry next iteration
                        }
                    }
                }
            }

            spinner.finish_with_message("All checks resolved");
            println!();
        }
    }

    // Check readiness if not forcing
    if !force {
        let mut issues = Vec::new();
        for pr in &prs_to_merge {
            if !pr.approved {
                issues.push(format!(
                    "{} PR #{}: not approved",
                    pr.repo_name, pr.pr_number
                ));
            }
            match pr.check_status {
                CheckStatus::Failing => {
                    issues.push(format!(
                        "{} PR #{}: checks failing",
                        pr.repo_name, pr.pr_number
                    ));
                }
                CheckStatus::Pending => {
                    issues.push(format!(
                        "{} PR #{}: checks still running",
                        pr.repo_name, pr.pr_number
                    ));
                }
                CheckStatus::Unknown => {
                    // Don't block on unknown - warn but allow merge
                    Output::warning(&format!(
                        "{} PR #{}: check status unknown - proceeding with caution",
                        pr.repo_name, pr.pr_number
                    ));
                }
                CheckStatus::Passing => {} // All good
            }
            if !pr.mergeable {
                issues.push(format!(
                    "{} PR #{}: not mergeable (branch may be behind base — try --update)",
                    pr.repo_name, pr.pr_number
                ));
            }
        }

        if !issues.is_empty() {
            Output::warning("Some PRs have issues:");
            for issue in &issues {
                println!("  - {}", issue);
            }
            println!();
            println!("Use --force to merge anyway.");
            return Ok(());
        }
    }

    // Auto-merge flow: enable auto-merge and return early
    if auto {
        let mut success_count = 0;
        let mut error_count = 0;

        for pr in prs_to_merge {
            let spinner = Output::spinner(&format!(
                "Enabling auto-merge for {} PR #{}...",
                pr.repo_name, pr.pr_number
            ));

            match pr
                .platform
                .enable_auto_merge(&pr.owner, &pr.repo, pr.pr_number, Some(merge_method))
                .await
            {
                Ok(true) => {
                    spinner.finish_with_message(format!(
                        "{}: PR #{} will auto-merge when checks pass",
                        pr.repo_name, pr.pr_number
                    ));
                    success_count += 1;
                }
                Ok(false) => {
                    spinner.finish_with_message(format!(
                        "{}: PR #{} auto-merge could not be enabled",
                        pr.repo_name, pr.pr_number
                    ));
                    error_count += 1;
                }
                Err(e) => {
                    spinner.finish_with_message(format!("{}: failed - {}", pr.repo_name, e));
                    error_count += 1;
                }
            }
        }

        println!();
        if error_count == 0 {
            Output::success(&format!(
                "Auto-merge enabled for {} PR(s). They will merge when all checks pass.",
                success_count
            ));
        } else {
            Output::warning(&format!(
                "{} auto-merge enabled, {} failed",
                success_count, error_count
            ));
        }

        return Ok(());
    }

    // Merge PRs
    let mut success_count = 0;
    let mut error_count = 0;

    #[derive(serde::Serialize)]
    struct JsonMergedPr {
        repo: String,
        pr_number: u64,
    }
    #[derive(serde::Serialize)]
    struct JsonFailedPr {
        repo: String,
        pr_number: u64,
        reason: String,
    }
    let mut json_merged: Vec<JsonMergedPr> = Vec::new();
    let mut json_failed_prs: Vec<JsonFailedPr> = Vec::new();

    for pr in prs_to_merge {
        let spinner = if !json {
            Some(Output::spinner(&format!(
                "Merging {} PR #{}...",
                pr.repo_name, pr.pr_number
            )))
        } else {
            None
        };

        let merge_result = pr
            .platform
            .merge_pull_request(
                &pr.owner,
                &pr.repo,
                pr.pr_number,
                Some(merge_method),
                true, // delete branch
            )
            .await;

        // Handle BranchBehind with --update retry
        let merge_result = match merge_result {
            Err(PlatformError::BranchBehind(ref msg)) if update => {
                if let Some(ref s) = spinner {
                    s.finish_with_message(format!(
                        "{}: branch behind base, updating...",
                        pr.repo_name
                    ));
                }
                let update_spinner = if !json {
                    Some(Output::spinner(&format!(
                        "Updating {} PR #{} branch...",
                        pr.repo_name, pr.pr_number
                    )))
                } else {
                    None
                };

                match pr
                    .platform
                    .update_branch(&pr.owner, &pr.repo, pr.pr_number)
                    .await
                {
                    Ok(true) => {
                        if let Some(ref s) = update_spinner {
                            s.finish_with_message(format!(
                                "{}: branch updated, retrying merge...",
                                pr.repo_name
                            ));
                        }
                        tokio::time::sleep(std::time::Duration::from_secs(3)).await;

                        let retry_spinner = if !json {
                            Some(Output::spinner(&format!(
                                "Merging {} PR #{}...",
                                pr.repo_name, pr.pr_number
                            )))
                        } else {
                            None
                        };

                        match pr
                            .platform
                            .merge_pull_request(
                                &pr.owner,
                                &pr.repo,
                                pr.pr_number,
                                Some(merge_method),
                                true,
                            )
                            .await
                        {
                            Ok(merged) => {
                                let verified = match pr
                                    .platform
                                    .get_pull_request(&pr.owner, &pr.repo, pr.pr_number)
                                    .await
                                {
                                    Ok(verified_pr) => verified_pr.merged,
                                    Err(_) => merged,
                                };

                                if verified {
                                    if let Some(ref s) = retry_spinner {
                                        s.finish_with_message(format!(
                                            "{}: merged PR #{}",
                                            pr.repo_name, pr.pr_number
                                        ));
                                    }
                                    success_count += 1;
                                    json_merged.push(JsonMergedPr {
                                        repo: pr.repo_name.clone(),
                                        pr_number: pr.pr_number,
                                    });
                                } else if merged {
                                    if let Some(ref s) = retry_spinner {
                                        s.finish_with_message(format!(
                                            "{}: PR #{} merge reported success but PR is not merged",
                                            pr.repo_name, pr.pr_number
                                        ));
                                    }
                                    error_count += 1;
                                    json_failed_prs.push(JsonFailedPr {
                                        repo: pr.repo_name.clone(),
                                        pr_number: pr.pr_number,
                                        reason: "merge reported success but PR is not merged"
                                            .to_string(),
                                    });
                                } else {
                                    if let Some(ref s) = retry_spinner {
                                        s.finish_with_message(format!(
                                            "{}: PR #{} was already merged",
                                            pr.repo_name, pr.pr_number
                                        ));
                                    }
                                    success_count += 1;
                                    json_merged.push(JsonMergedPr {
                                        repo: pr.repo_name.clone(),
                                        pr_number: pr.pr_number,
                                    });
                                }
                                continue;
                            }
                            Err(e) => Err(e),
                        }
                    }
                    Ok(false) => {
                        if let Some(ref s) = update_spinner {
                            s.finish_with_message(format!(
                                "{}: branch already up to date",
                                pr.repo_name
                            ));
                        }
                        Err(PlatformError::BranchBehind(msg.clone()))
                    }
                    Err(update_err) => {
                        if let Some(ref s) = update_spinner {
                            s.finish_with_message(format!(
                                "{}: branch update failed - {}",
                                pr.repo_name, update_err
                            ));
                        }
                        Err(PlatformError::BranchBehind(msg.clone()))
                    }
                }
            }
            other => other,
        };

        match merge_result {
            Ok(merged) => {
                let verified = match pr
                    .platform
                    .get_pull_request(&pr.owner, &pr.repo, pr.pr_number)
                    .await
                {
                    Ok(verified_pr) => verified_pr.merged,
                    Err(_) => merged,
                };

                if verified {
                    if let Some(ref s) = spinner {
                        s.finish_with_message(format!(
                            "{}: merged PR #{}",
                            pr.repo_name, pr.pr_number
                        ));
                    }
                    success_count += 1;
                    json_merged.push(JsonMergedPr {
                        repo: pr.repo_name.clone(),
                        pr_number: pr.pr_number,
                    });
                } else if merged {
                    if let Some(ref s) = spinner {
                        s.finish_with_message(format!(
                            "{}: PR #{} merge reported success but PR is not merged — check branch protection or required checks",
                            pr.repo_name, pr.pr_number
                        ));
                    }
                    error_count += 1;
                    json_failed_prs.push(JsonFailedPr {
                        repo: pr.repo_name.clone(),
                        pr_number: pr.pr_number,
                        reason: "merge reported success but PR is not merged".to_string(),
                    });
                } else {
                    if let Some(ref s) = spinner {
                        s.finish_with_message(format!(
                            "{}: PR #{} was already merged",
                            pr.repo_name, pr.pr_number
                        ));
                    }
                    success_count += 1;
                    json_merged.push(JsonMergedPr {
                        repo: pr.repo_name.clone(),
                        pr_number: pr.pr_number,
                    });
                }
            }
            Err(PlatformError::BranchBehind(_)) => {
                if let Some(ref s) = spinner {
                    s.finish_with_message(format!(
                        "{}: PR #{} branch is behind base branch",
                        pr.repo_name, pr.pr_number
                    ));
                }
                if !json {
                    Output::info(
                        "  Hint: use 'gr pr merge --update' to update the branch and retry",
                    );
                }
                error_count += 1;
                json_failed_prs.push(JsonFailedPr {
                    repo: pr.repo_name.clone(),
                    pr_number: pr.pr_number,
                    reason: "branch is behind base branch".to_string(),
                });
            }
            Err(PlatformError::BranchProtected(ref msg)) => {
                if let Some(ref s) = spinner {
                    s.finish_with_message(format!("{}: {}", pr.repo_name, msg));
                }
                if !json {
                    Output::info(
                        "  Hint: use 'gr pr merge --auto' to enable auto-merge when checks pass",
                    );
                    Output::info(&format!(
                        "  Or:   gh pr merge {} --admin --repo {}/{}",
                        pr.pr_number, pr.owner, pr.repo
                    ));
                }
                error_count += 1;
                json_failed_prs.push(JsonFailedPr {
                    repo: pr.repo_name.clone(),
                    pr_number: pr.pr_number,
                    reason: msg.clone(),
                });
            }
            Err(e) => {
                if let Some(ref s) = spinner {
                    s.finish_with_message(format!("{}: failed - {}", pr.repo_name, e));
                }
                json_failed_prs.push(JsonFailedPr {
                    repo: pr.repo_name.clone(),
                    pr_number: pr.pr_number,
                    reason: e.to_string(),
                });
                error_count += 1;

                if !force
                    && manifest.settings.merge_strategy
                        == crate::core::manifest::MergeStrategy::AllOrNothing
                {
                    if !json {
                        Output::error(
                            "Stopping due to all-or-nothing merge strategy. Use --force to bypass.",
                        );
                    }
                    return Err(e.into());
                }
                if force
                    && manifest.settings.merge_strategy
                        == crate::core::manifest::MergeStrategy::AllOrNothing
                    && !json
                {
                    Output::warning(&format!(
                        "{}: merge failed but continuing due to --force flag",
                        pr.repo_name
                    ));
                }
            }
        }
    }

    // Summary
    if json {
        #[derive(serde::Serialize)]
        struct JsonPrMergeResult {
            success: bool,
            merged: Vec<JsonMergedPr>,
            failed: Vec<JsonFailedPr>,
            skipped: Vec<String>,
        }

        let result = JsonPrMergeResult {
            success: error_count == 0,
            merged: json_merged,
            failed: json_failed_prs,
            skipped: json_skipped,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if error_count == 0 {
            Output::success(&format!("Successfully merged {} PR(s).", success_count));
        } else {
            Output::warning(&format!("{} merged, {} failed", success_count, error_count));
        }
    }

    Ok(())
}

/// Check if a repo has changes ahead of its default branch
/// Returns Ok(true) if there are changes, Ok(false) if no changes or on default branch
fn check_repo_for_changes(repo: &RepoInfo) -> anyhow::Result<bool> {
    let git_repo = open_repo(&repo.absolute_path)
        .map_err(|e| anyhow::anyhow!("Failed to open repo: {}", e))?;

    let current = get_current_branch(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to get current branch: {}", e))?;

    // Skip if on target branch
    if current == repo.target_branch() {
        return Ok(false);
    }

    // Check for commits ahead of target branch using shared helper
    has_commits_ahead(&git_repo, &current, repo.target_branch())
}
