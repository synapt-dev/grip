//! PR status command implementation

use crate::cli::output::{Output, Table};
use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use std::path::Path;

/// Run the PR status command
pub async fn run_pr_status(
    workspace_root: &Path,
    manifest: &Manifest,
    json_output: bool,
) -> anyhow::Result<()> {
    if !json_output {
        Output::header("Pull Request Status");
        let effective_target = manifest
            .settings
            .target
            .as_deref()
            .or(manifest.settings.revision.as_deref())
            .unwrap_or("main");
        if effective_target != "main" {
            Output::info(&format!("Target: {}", effective_target));
        }
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
        .filter(|r| !r.reference) // Skip reference repos
        .collect();

    #[derive(serde::Serialize)]
    struct PRStatusInfo {
        repo: String,
        branch: String,
        pr_number: Option<u64>,
        state: String,
        approved: bool,
        checks_pass: bool,
        mergeable: bool,
        url: Option<String>,
    }

    let mut statuses: Vec<PRStatusInfo> = Vec::new();

    for repo in &repos {
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
                // Get PR details including approval and mergeable status
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
                let checks_pass = match platform
                    .get_status_checks(&repo.owner, &repo.repo, &branch)
                    .await
                {
                    Ok(status) => status.state == crate::platform::CheckState::Success,
                    Err(_) => false,
                };

                statuses.push(PRStatusInfo {
                    repo: repo.name.clone(),
                    branch: branch.clone(),
                    pr_number: Some(pr.number),
                    state: "open".to_string(),
                    approved,
                    checks_pass,
                    mergeable,
                    url: Some(pr.url.clone()),
                });
            }
            Ok(None) => {
                statuses.push(PRStatusInfo {
                    repo: repo.name.clone(),
                    branch: branch.clone(),
                    pr_number: None,
                    state: "none".to_string(),
                    approved: false,
                    checks_pass: false,
                    mergeable: false,
                    url: None,
                });
            }
            Err(e) => {
                if !json_output {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
            }
        }
    }

    if json_output {
        println!("{}", serde_json::to_string_pretty(&statuses)?);
        return Ok(());
    }

    if statuses.is_empty() {
        println!("No repositories have changes on feature branches.");
        return Ok(());
    }

    // Display table
    let mut table = Table::new(vec![
        "Repo",
        "PR#",
        "State",
        "Approved",
        "Checks",
        "Mergeable",
    ]);

    for status in &statuses {
        let pr_num = status
            .pr_number
            .map(|n| format!("#{}", n))
            .unwrap_or_else(|| "-".to_string());
        let approved = if status.approved { "✓" } else { "✗" };
        let checks = if status.checks_pass { "✓" } else { "✗" };
        let mergeable = if status.mergeable { "✓" } else { "✗" };

        table.add_row(vec![
            &status.repo,
            &pr_num,
            &status.state,
            approved,
            checks,
            mergeable,
        ]);
    }

    table.print();

    // Summary
    println!();
    let with_prs = statuses.iter().filter(|s| s.pr_number.is_some()).count();
    let ready = statuses
        .iter()
        .filter(|s| s.pr_number.is_some() && s.approved && s.checks_pass && s.mergeable)
        .count();

    if ready == with_prs && with_prs > 0 {
        Output::success(&format!("All {} PRs ready to merge!", with_prs));
    } else if with_prs > 0 {
        println!("{}/{} PRs ready to merge", ready, with_prs);
    }

    Ok(())
}
