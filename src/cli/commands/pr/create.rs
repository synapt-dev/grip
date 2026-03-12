//! PR create command implementation

use std::collections::BTreeMap;
use std::path::Path;

use git2::Repository;

use crate::cli::output::Output;
use crate::core::manifest::{Manifest, PlatformType};
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::core::state::StateFile;
use crate::git::status::has_uncommitted_changes;
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;

/// A group of repos all on the same feature branch
struct BranchGroup {
    branch: String,
    repos: Vec<RepoInfo>,
}

/// Convert a branch name to a PR title
fn branch_to_title(branch: &str) -> String {
    let title = branch
        .trim_start_matches("feat/")
        .trim_start_matches("fix/")
        .trim_start_matches("chore/")
        .replace(['-', '_'], " ");
    let mut chars = title.chars();
    match chars.next() {
        None => title,
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

/// Run the PR create command
#[allow(clippy::too_many_arguments)]
pub async fn run_pr_create(
    workspace_root: &Path,
    manifest: &Manifest,
    title: Option<&str>,
    body: Option<&str>,
    draft: bool,
    push_first: bool,
    dry_run: bool,
    repo_filter: Option<&[String]>,
    json: bool,
) -> anyhow::Result<()> {
    if !json {
        if dry_run {
            Output::header("PR Preview");
            println!();
        } else {
            Output::header("Creating pull requests...");
            println!();
        }
    }

    let repos = filter_repos(manifest, workspace_root, repo_filter, None, false);

    // Validate repo filter
    if let Some(filter) = repo_filter {
        let repo_names: Vec<&str> = repos.iter().map(|r| r.name.as_str()).collect();
        for name in filter {
            if !repo_names.contains(&name.as_str()) {
                anyhow::bail!("Repository '{}' not found in manifest", name);
            }
        }
    }

    // Group repos by their current feature branch
    let mut branch_groups: BTreeMap<String, Vec<RepoInfo>> = BTreeMap::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let current = match get_current_branch(&git_repo) {
                    Ok(b) => b,
                    Err(_) => continue,
                };

                // Skip if on target branch
                if current == repo.target_branch() {
                    continue;
                }

                // Check for changes ahead of target branch
                if has_commits_ahead(&git_repo, &current, repo.target_branch())? {
                    branch_groups.entry(current).or_default().push(repo.clone());
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    // Also check the manifest repo for changes (if not excluded by repo filter)
    let include_manifest = repo_filter
        .map(|f| f.iter().any(|n| n == "manifest"))
        .unwrap_or(true);

    if include_manifest {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            match check_manifest_repo_branch(&manifest_repo) {
                Ok(Some((branch, repo_info))) => {
                    branch_groups.entry(branch).or_default().push(repo_info);
                }
                Ok(None) => {}
                Err(e) => {
                    Output::warning(&format!("Could not check manifest repo: {}", e));
                }
            }
        }
    }

    if branch_groups.is_empty() {
        if !json {
            println!("No repositories have changes to create PRs for.");
        } else {
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "success": true,
                    "prs": [],
                    "failed": []
                }))?
            );
        }
        return Ok(());
    }

    // Build ordered branch groups
    let groups: Vec<BranchGroup> = branch_groups
        .into_iter()
        .map(|(branch, repos)| BranchGroup { branch, repos })
        .collect();

    let multi_branch = groups.len() > 1;

    // All results across all branch groups
    let mut all_created_prs: Vec<(String, String, u64, String)> = Vec::new(); // (branch, repo, number, url)
    let mut all_failed_repos: Vec<(String, String)> = Vec::new(); // (repo, error)

    for group in &groups {
        let pr_title = title
            .map(|s| s.to_string())
            .unwrap_or_else(|| branch_to_title(&group.branch));

        if multi_branch && !json {
            Output::subheader(&format!("Branch: {}", group.branch));
        }

        // Push if requested (skip for preview)
        if push_first && !dry_run {
            if !multi_branch {
                Output::info("Pushing branches first...");
            }
            for repo in &group.repos {
                if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                    let spinner = Output::spinner(&format!("Pushing {}...", repo.name));
                    match crate::git::remote::push_branch(
                        &git_repo,
                        &group.branch,
                        &repo.push_remote,
                        true,
                    ) {
                        Ok(()) => spinner.finish_with_message(format!("{}: pushed", repo.name)),
                        Err(e) => spinner
                            .finish_with_message(format!("{}: push failed - {}", repo.name, e)),
                    }
                }
            }
            println!();
        }

        // Preview mode
        if dry_run {
            Output::info(&format!("Branch: {}", group.branch));
            Output::info(&format!("Title: {}", pr_title));
            if let Some(pr_body) = body {
                Output::info(&format!("Body: {}", pr_body));
            }
            if draft {
                Output::info("Type: Draft PR");
            }
            println!();

            Output::subheader("Repositories that would create PRs:");
            for repo in &group.repos {
                println!(
                    "  - {} ({}/{}) → {}",
                    repo.name,
                    repo.owner,
                    repo.repo,
                    repo.target_branch()
                );
            }
            println!();
            continue;
        }

        // Create PRs for each repo in this branch group
        for repo in &group.repos {
            let platform =
                get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

            let spinner = Output::spinner(&format!("Creating PR for {}...", repo.name));

            match platform
                .create_pull_request(
                    &repo.owner,
                    &repo.repo,
                    &group.branch,
                    repo.target_branch(),
                    &pr_title,
                    body,
                    draft,
                )
                .await
            {
                Ok(pr) => {
                    spinner.finish_with_message(format!(
                        "{}: created PR #{} - {}",
                        repo.name, pr.number, pr.url
                    ));
                    all_created_prs.push((
                        group.branch.clone(),
                        repo.name.clone(),
                        pr.number,
                        pr.url.clone(),
                    ));
                }
                Err(e) => {
                    spinner.finish_with_message(format!("{}: failed - {}", repo.name, e));
                    all_failed_repos.push((repo.name.clone(), e.to_string()));
                }
            }
        }

        // Save state per branch
        if all_created_prs
            .iter()
            .any(|(b, _, _, _)| b == &group.branch)
        {
            let state_path = workspace_root.join(".gitgrip").join("state.json");
            let mut state = if state_path.exists() {
                let content = std::fs::read_to_string(&state_path)?;
                StateFile::parse(&content).unwrap_or_default()
            } else {
                StateFile::default()
            };

            if let Some((_, _, first_pr_number, _)) = all_created_prs
                .iter()
                .find(|(b, _, _, _)| b == &group.branch)
            {
                state.set_pr_for_branch(&group.branch, *first_pr_number);
            }

            let state_json = serde_json::to_string_pretty(&state)?;
            std::fs::write(&state_path, state_json)?;
        }
    }

    // Summary
    if dry_run {
        if !json {
            Output::warning("Run without --dry-run to actually create the PRs.");
        }
        return Ok(());
    }

    if json {
        #[derive(serde::Serialize)]
        struct JsonPrCreateResult {
            success: bool,
            prs: Vec<JsonCreatedPr>,
            failed: Vec<JsonFailedRepo>,
        }
        #[derive(serde::Serialize)]
        struct JsonCreatedPr {
            repo: String,
            branch: String,
            number: u64,
            url: String,
        }
        #[derive(serde::Serialize)]
        struct JsonFailedRepo {
            repo: String,
            reason: String,
        }

        let result = JsonPrCreateResult {
            success: !all_created_prs.is_empty() && all_failed_repos.is_empty(),
            prs: all_created_prs
                .iter()
                .map(|(branch, repo, number, url)| JsonCreatedPr {
                    repo: repo.clone(),
                    branch: branch.clone(),
                    number: *number,
                    url: url.clone(),
                })
                .collect(),
            failed: all_failed_repos
                .iter()
                .map(|(repo, reason)| JsonFailedRepo {
                    repo: repo.clone(),
                    reason: reason.clone(),
                })
                .collect(),
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if all_created_prs.is_empty() && all_failed_repos.is_empty() {
            Output::warning("No PRs were created.");
        } else {
            if !all_created_prs.is_empty() {
                Output::success(&format!("Created {} PR(s):", all_created_prs.len()));
                for (branch, repo_name, pr_number, url) in &all_created_prs {
                    if multi_branch {
                        println!("  {} ({}): #{} - {}", repo_name, branch, pr_number, url);
                    } else {
                        println!("  {}: #{} - {}", repo_name, pr_number, url);
                    }
                }
            }
            if !all_failed_repos.is_empty() {
                if !all_created_prs.is_empty() {
                    println!();
                }
                Output::error(&format!(
                    "Failed to create {} PR(s):",
                    all_failed_repos.len()
                ));
                for (repo_name, error) in &all_failed_repos {
                    println!("  {}: {}", repo_name, error);
                }
            }
        }
    }

    Ok(())
}

/// Check if a branch has commits ahead of another branch
pub(crate) fn has_commits_ahead(
    repo: &Repository,
    branch: &str,
    base: &str,
) -> anyhow::Result<bool> {
    let local_ref = format!("refs/heads/{}", branch);
    let base_ref = format!("refs/remotes/origin/{}", base);

    let local = match repo.find_reference(&local_ref) {
        Ok(r) => r,
        Err(_) => return Ok(false),
    };

    let base_branch = match repo.find_reference(&base_ref) {
        Ok(r) => r,
        Err(_) => {
            // Try local base branch
            match repo.find_reference(&format!("refs/heads/{}", base)) {
                Ok(r) => r,
                // Neither remote nor local base ref exists — assume the branch
                // has changes worth including (e.g. repo hasn't fetched yet).
                Err(_) => return Ok(true),
            }
        }
    };

    let local_oid = local.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve branch '{}'. Ensure it exists and has at least one commit.",
            branch
        )
    })?;
    let base_oid = base_branch.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve base branch '{}'. Ensure it exists and has at least one commit.",
            base
        )
    })?;

    let (ahead, _behind) = repo.graph_ahead_behind(local_oid, base_oid)?;
    Ok(ahead > 0)
}

/// Check if the manifest repo has changes and return its branch name
fn check_manifest_repo_branch(repo: &RepoInfo) -> anyhow::Result<Option<(String, RepoInfo)>> {
    let git_repo = open_repo(&repo.absolute_path)
        .map_err(|e| anyhow::anyhow!("Failed to open repo: {}", e))?;

    let current = get_current_branch(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to get current branch: {}", e))?;

    // Skip if on target branch
    if current == repo.target_branch() {
        return Ok(None);
    }

    let has_commits = has_commits_ahead(&git_repo, &current, repo.target_branch())
        .map_err(|e| anyhow::anyhow!("Failed to check commits: {}", e))?;

    let has_uncommitted = has_uncommitted_changes(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to check uncommitted changes: {}", e))?;

    if has_commits || has_uncommitted {
        Ok(Some((current, repo.clone())))
    } else {
        Ok(None)
    }
}

/// Get authentication token for platform
#[allow(dead_code)]
pub fn get_token_for_platform(platform: &PlatformType) -> Option<String> {
    match platform {
        PlatformType::GitHub => std::env::var("GITHUB_TOKEN")
            .ok()
            .or_else(|| std::env::var("GH_TOKEN").ok()),
        PlatformType::GitLab => std::env::var("GITLAB_TOKEN").ok(),
        PlatformType::AzureDevOps => std::env::var("AZURE_DEVOPS_TOKEN").ok(),
        PlatformType::Bitbucket => std::env::var("BITBUCKET_TOKEN").ok(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn setup_test_repo() -> (TempDir, Repository) {
        let temp = TempDir::new().unwrap();

        Command::new("git")
            .args(["init"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Create initial commit on main
        fs::write(temp.path().join("README.md"), "# Test").unwrap();
        Command::new("git")
            .args(["add", "README.md"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Initial commit"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let repo = crate::git::open_repo(temp.path()).unwrap();
        (temp, repo)
    }

    #[test]
    fn test_has_commits_ahead_returns_true_when_no_base_refs_exist() {
        let (temp, repo) = setup_test_repo();

        // Create a feature branch with a commit
        Command::new("git")
            .args(["checkout", "-b", "feat/test"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        fs::write(temp.path().join("feature.txt"), "new feature").unwrap();
        Command::new("git")
            .args(["add", "feature.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Add feature"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Check against a base branch that doesn't exist locally or remotely.
        // This simulates the scenario where origin/main hasn't been fetched.
        let result = has_commits_ahead(&repo, "feat/test", "nonexistent-base");
        assert!(result.is_ok());
        assert!(
            result.unwrap(),
            "Should return true when base refs are missing"
        );
    }

    #[test]
    fn test_has_commits_ahead_with_local_base() {
        let (temp, repo) = setup_test_repo();

        // Get the default branch name
        let output = Command::new("git")
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        let default_branch = String::from_utf8_lossy(&output.stdout).trim().to_string();

        // Create feature branch with a commit
        Command::new("git")
            .args(["checkout", "-b", "feat/test"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        fs::write(temp.path().join("feature.txt"), "new feature").unwrap();
        Command::new("git")
            .args(["add", "feature.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Add feature"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Local base ref exists — should detect the commit ahead
        let result = has_commits_ahead(&repo, "feat/test", &default_branch);
        assert!(result.is_ok());
        assert!(result.unwrap(), "Should detect commits ahead of local base");
    }

    #[test]
    fn test_has_commits_ahead_same_commit() {
        let (temp, repo) = setup_test_repo();

        let output = Command::new("git")
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        let default_branch = String::from_utf8_lossy(&output.stdout).trim().to_string();

        // Create feature branch but don't add any commits
        Command::new("git")
            .args(["checkout", "-b", "feat/no-changes"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = has_commits_ahead(&repo, "feat/no-changes", &default_branch);
        assert!(result.is_ok());
        assert!(
            !result.unwrap(),
            "Should return false when no commits ahead"
        );
    }

    #[test]
    fn test_branch_to_title() {
        assert_eq!(branch_to_title("feat/my-feature"), "My feature");
        assert_eq!(branch_to_title("fix/bug-fix"), "Bug fix");
        assert_eq!(branch_to_title("chore/cleanup_task"), "Cleanup task");
        assert_eq!(branch_to_title("custom-branch"), "Custom branch");
    }
}
