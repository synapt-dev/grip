//! PR checks command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::{get_platform_adapter, CheckState};
use std::path::PathBuf;

/// Run the PR checks command
pub async fn run_pr_checks(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    json_output: bool,
) -> anyhow::Result<()> {
    if !json_output {
        Output::header("CI/CD Check Status");
        println!();
    }

    let repos: Vec<RepoInfo> = manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(name, config, workspace_root, &manifest.settings)
        })
        .collect();

    #[derive(serde::Serialize)]
    struct CheckInfo {
        context: String,
        state: String,
    }

    #[derive(serde::Serialize)]
    struct RepoChecks {
        repo: String,
        pr_number: Option<u64>,
        overall_state: String,
        checks: Vec<CheckInfo>,
    }

    let mut all_checks: Vec<RepoChecks> = Vec::new();
    let mut total_passed = 0;
    let mut total_failed = 0;
    let mut total_pending = 0;

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

        // Find PR number (optional, for display)
        let pr_number = match platform
            .find_pr_by_branch(&repo.owner, &repo.repo, &branch)
            .await
        {
            Ok(Some(pr)) => Some(pr.number),
            _ => None,
        };

        // Get status checks for the branch
        match platform
            .get_status_checks(&repo.owner, &repo.repo, &branch)
            .await
        {
            Ok(status_result) => {
                let check_infos: Vec<CheckInfo> = status_result
                    .statuses
                    .iter()
                    .map(|s| {
                        let state = s.state.to_lowercase();
                        match state.as_str() {
                            "success" => total_passed += 1,
                            "failure" | "error" => total_failed += 1,
                            _ => total_pending += 1,
                        }
                        CheckInfo {
                            context: s.context.clone(),
                            state,
                        }
                    })
                    .collect();

                if !json_output {
                    // Print status with indicator
                    let overall = match status_result.state {
                        CheckState::Success => "✓",
                        CheckState::Failure => "✗",
                        CheckState::Pending => "●",
                    };

                    let pr_str = pr_number.map(|n| format!(" #{}", n)).unwrap_or_default();
                    println!("{} {}{}", overall, repo.name, pr_str);

                    for check in &check_infos {
                        let indicator = match check.state.as_str() {
                            "success" => "  ✓",
                            "failure" | "error" => "  ✗",
                            _ => "  ●",
                        };
                        println!("  {} {} {}", indicator, check.context, check.state);
                    }
                    println!();
                }

                all_checks.push(RepoChecks {
                    repo: repo.name.clone(),
                    pr_number,
                    overall_state: format!("{:?}", status_result.state).to_lowercase(),
                    checks: check_infos,
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
        println!("{}", serde_json::to_string_pretty(&all_checks)?);
        return Ok(());
    }

    // Summary
    if total_passed + total_failed + total_pending > 0 {
        println!(
            "Summary: {} passed, {} failed, {} pending",
            total_passed, total_failed, total_pending
        );

        if total_failed > 0 {
            Output::warning("Some checks are failing. PR cannot be merged.");
        } else if total_pending > 0 {
            Output::info("Some checks are still pending.");
            println!();
            Output::info("Note: GitHub may require additional CI checks (e.g., 'CI' workflow)");
            Output::info(
                "that are not visible here. Wait for all checks to complete before merging.",
            );
        } else {
            Output::success("All checks passing!");
        }
    }

    Ok(())
}
