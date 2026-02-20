//! Status command implementation

use crate::cli::output::{Output, Table};
use crate::core::gripspace::{get_gripspace_rev, gripspace_name};
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::path_exists;
use crate::git::status::{get_repo_status, RepoStatus};
use std::path::PathBuf;

/// JSON-serializable repo status for --json output
#[derive(serde::Serialize)]
struct JsonRepoStatus {
    name: String,
    branch: String,
    clean: bool,
    staged: usize,
    modified: usize,
    untracked: usize,
    ahead: usize,
    behind: usize,
    reference: bool,
    groups: Vec<String>,
}

/// Run the status command
pub fn run_status(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    verbose: bool,
    quiet: bool,
    group_filter: Option<&[String]>,
    json: bool,
) -> anyhow::Result<()> {
    if json {
        return run_status_json(workspace_root, manifest, group_filter);
    }

    Output::header("Repository Status");
    println!();

    // Get all repo info (include reference repos for display)
    let repos: Vec<RepoInfo> = filter_repos(manifest, workspace_root, None, group_filter, true);

    // Get status for all repos
    let statuses: Vec<(RepoStatus, &RepoInfo)> = repos
        .iter()
        .map(|repo| (get_repo_status(repo), repo))
        .collect();

    // Count stats
    let total = statuses.len();
    let cloned = statuses.iter().filter(|(s, _)| s.exists).count();
    let with_changes = statuses.iter().filter(|(s, _)| !s.clean).count();
    let ahead_count = statuses.iter().filter(|(s, _)| s.ahead_main > 0).count();

    // In quiet mode, only show repos with changes or not on default branch
    let filtered_statuses: Vec<&(RepoStatus, &RepoInfo)> = if quiet {
        statuses
            .iter()
            .filter(|(s, repo)| !s.clean || !s.exists || s.branch != repo.target_branch())
            .collect()
    } else {
        statuses.iter().collect()
    };

    // Display table
    let mut table = Table::new(vec!["Repo", "Branch", "Status", "vs target"]);

    for (status, repo) in &filtered_statuses {
        let status_str = format_status(status, verbose);
        let target_str = format_target_comparison(status, repo.target_branch());
        // Add [ref] suffix for reference repos
        let repo_display = if repo.reference {
            format!("{} [ref]", Output::repo_name(&status.name))
        } else {
            Output::repo_name(&status.name)
        };
        table.add_row(vec![
            &repo_display,
            &Output::branch_name(&status.branch),
            &status_str,
            &target_str,
        ]);
    }

    if !filtered_statuses.is_empty() {
        table.print();
    }

    // Show manifest worktree status if it exists
    if let Some(manifests_dir) = manifest_paths::resolve_manifest_repo_dir(workspace_root) {
        let manifests_git_dir = manifests_dir.join(".git");
        if manifests_git_dir.exists() && path_exists(&manifests_dir) {
            println!();
            let rel_path = manifests_dir
                .strip_prefix(workspace_root)
                .ok()
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_else(|| manifest_paths::MAIN_SPACE_DIR.to_string());
            // Create a minimal RepoInfo for the manifest
            let manifest_repo_info = RepoInfo {
                name: "manifest".to_string(),
                url: String::new(),
                path: rel_path,
                absolute_path: manifests_dir.clone(),
                revision: "main".to_string(),
                target: "main".to_string(),
                sync_remote: "origin".to_string(),
                push_remote: "origin".to_string(),
                owner: String::new(),
                repo: "manifests".to_string(),
                platform_type: crate::core::manifest::PlatformType::GitHub,
                platform_base_url: None,
                project: None,
                reference: false,
                groups: Vec::new(),
                agent: None,
            };

            let status = get_repo_status(&manifest_repo_info);
            let status_str = format_status(&status, verbose);
            let target_str = format_target_comparison(&status, manifest_repo_info.target_branch());
            let mut manifest_table = Table::new(vec!["Repo", "Branch", "Status", "vs target"]);
            manifest_table.add_row(vec![
                &Output::repo_name("manifest"),
                &Output::branch_name(&status.branch),
                &status_str,
                &target_str,
            ]);
            manifest_table.print();
        }
    }

    // Show gripspace status
    if let Some(ref gripspaces) = manifest.gripspaces {
        if !gripspaces.is_empty() && !quiet {
            let spaces_dir = manifest_paths::spaces_dir(workspace_root);
            println!();
            let mut gs_table = Table::new(vec!["Gripspace", "Rev", "Status"]);

            for gs in gripspaces {
                let name = gripspace_name(&gs.url);
                let dir_name =
                    match crate::core::gripspace::resolve_space_name(&gs.url, &spaces_dir) {
                        Ok(dir_name) => dir_name,
                        Err(e) => {
                            let rev = "—".to_string();
                            // Extract the inner message from ManifestError::GripspaceError
                            let msg = match &e {
                                crate::core::manifest::ManifestError::GripspaceError(msg) => {
                                    msg.clone()
                                }
                                other => other.to_string(),
                            };
                            let status_str = format!("error: {}", msg);
                            gs_table.add_row(vec![&Output::repo_name(&name), &rev, &status_str]);
                            continue;
                        }
                    };
                let gs_path = spaces_dir.join(&dir_name);

                let (rev, status_str) = if gs_path.exists() {
                    let rev = get_gripspace_rev(&gs_path).unwrap_or_else(|| "unknown".to_string());
                    let pinned = gs
                        .rev
                        .as_deref()
                        .map(|r| format!(" (pinned: {})", r))
                        .unwrap_or_default();
                    (rev, format!("✓{}", pinned))
                } else {
                    ("—".to_string(), "not cloned".to_string())
                };

                gs_table.add_row(vec![&Output::repo_name(&name), &rev, &status_str]);
            }

            gs_table.print();
        }
    }

    // Summary
    println!();
    if quiet {
        // Machine-readable summary line
        println!(
            "SUMMARY: repos={} cloned={} changes={} ahead={}",
            total, cloned, with_changes, ahead_count
        );
    } else {
        let ahead_suffix = if ahead_count > 0 {
            format!(" | {} ahead of main", ahead_count)
        } else {
            String::new()
        };
        println!(
            "  {}/{} cloned | {} with changes{}",
            cloned, total, with_changes, ahead_suffix
        );
    }

    Ok(())
}

/// Run status in JSON mode
fn run_status_json(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let repos: Vec<RepoInfo> = filter_repos(manifest, workspace_root, None, group_filter, true);

    let json_statuses: Vec<JsonRepoStatus> = repos
        .iter()
        .map(|repo| {
            let status = get_repo_status(repo);
            JsonRepoStatus {
                name: status.name,
                branch: status.branch,
                clean: status.clean,
                staged: status.staged,
                modified: status.modified,
                untracked: status.untracked,
                ahead: status.ahead_main,
                behind: status.behind_main,
                reference: repo.reference,
                groups: repo.groups.clone(),
            }
        })
        .collect();

    println!("{}", serde_json::to_string_pretty(&json_statuses)?);
    Ok(())
}

/// Format the vs target comparison column
fn format_target_comparison(status: &RepoStatus, target_branch: &str) -> String {
    // On target branch - no comparison needed
    if status.branch == target_branch {
        return "-".to_string();
    }

    if status.ahead_main == 0 && status.behind_main == 0 {
        return "\u{2713}".to_string(); // checkmark
    }

    let mut parts = Vec::new();
    if status.ahead_main > 0 {
        parts.push(format!("\u{2191}{}", status.ahead_main)); // up arrow
    }
    if status.behind_main > 0 {
        parts.push(format!("\u{2193}{}", status.behind_main)); // down arrow
    }
    parts.join(" ")
}

/// Format status for display
fn format_status(status: &RepoStatus, verbose: bool) -> String {
    if !status.exists {
        return "not cloned".to_string();
    }

    if status.clean {
        return "✓".to_string();
    }

    let mut parts = Vec::new();

    if status.staged > 0 {
        parts.push(format!("+{}", status.staged));
    }
    if status.modified > 0 {
        parts.push(format!("~{}", status.modified));
    }
    if status.untracked > 0 {
        parts.push(format!("?{}", status.untracked));
    }

    if verbose {
        if status.ahead > 0 {
            parts.push(format!("↑{}", status.ahead));
        }
        if status.behind > 0 {
            parts.push(format!("↓{}", status.behind));
        }
    }

    parts.join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_format_status_clean() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "main".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_status(&status, false), "✓");
    }

    #[test]
    fn test_format_status_changes() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "main".to_string(),
            clean: false,
            staged: 2,
            modified: 3,
            untracked: 1,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_status(&status, false), "+2 ~3 ?1");
    }

    #[test]
    fn test_format_status_ahead_behind() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "feat".to_string(),
            clean: false,
            staged: 1,
            modified: 0,
            untracked: 0,
            ahead: 3,
            behind: 1,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_status(&status, true), "+1 ↑3 ↓1");
    }

    #[test]
    fn test_format_target_comparison_on_main() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "main".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_target_comparison(&status, "main"), "-");
    }

    #[test]
    fn test_format_target_comparison_ahead() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "feat/test".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 5,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_target_comparison(&status, "main"), "↑5");
    }

    #[test]
    fn test_format_target_comparison_behind() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "feat/test".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 3,
            exists: true,
        };
        assert_eq!(format_target_comparison(&status, "main"), "↓3");
    }

    #[test]
    fn test_format_target_comparison_both() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "feat/test".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 2,
            behind_main: 5,
            exists: true,
        };
        assert_eq!(format_target_comparison(&status, "main"), "↑2 ↓5");
    }

    #[test]
    fn test_format_target_comparison_in_sync() {
        let status = RepoStatus {
            name: "test".to_string(),
            branch: "feat/test".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
        };
        assert_eq!(format_target_comparison(&status, "main"), "✓");
    }
}
