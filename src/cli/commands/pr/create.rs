//! PR create command implementation

use crate::cli::output::Output;
use crate::core::manifest::{Manifest, PlatformType};
use crate::core::repo::{get_manifest_repo_info, RepoInfo};
use crate::core::state::StateFile;
use crate::git::status::has_uncommitted_changes;
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use git2::Repository;
use std::path::PathBuf;

/// Run the PR create command
#[allow(clippy::too_many_arguments)]
pub async fn run_pr_create(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    title: Option<&str>,
    body: Option<&str>,
    draft: bool,
    push_first: bool,
    dry_run: bool,
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

    // Get current branch for all repos and verify consistency
    let mut branch_name: Option<String> = None;
    let mut repos_with_changes: Vec<RepoInfo> = Vec::new();

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
                    if let Some(ref bn) = branch_name {
                        if bn != &current {
                            anyhow::bail!(
                                "Repositories are on different branches: {} vs {}",
                                bn,
                                current
                            );
                        }
                    } else {
                        branch_name = Some(current);
                    }
                    repos_with_changes.push(repo.clone());
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    // Also check the manifest repo for changes
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        match check_repo_for_changes(&manifest_repo, &mut branch_name) {
            Ok(true) => {
                repos_with_changes.push(manifest_repo);
            }
            Ok(false) => {
                // No changes or on default branch - skip
            }
            Err(e) => {
                Output::warning(&format!("Could not check manifest repo: {}", e));
            }
        }
    }

    let branch = match branch_name {
        Some(b) => b,
        None => {
            println!("No repositories have changes to create PRs for.");
            return Ok(());
        }
    };

    // Get title from argument or use branch name as fallback
    let pr_title = title.map(|s| s.to_string()).unwrap_or_else(|| {
        // Convert branch name to title: feat/my-feature -> My feature
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
    });

    // Push if requested (skip for preview)
    if push_first && !dry_run {
        Output::info("Pushing branches first...");
        for repo in &repos_with_changes {
            if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                let spinner = Output::spinner(&format!("Pushing {}...", repo.name));
                match crate::git::remote::push_branch(&git_repo, &branch, &repo.push_remote, true) {
                    Ok(()) => spinner.finish_with_message(format!("{}: pushed", repo.name)),
                    Err(e) => {
                        spinner.finish_with_message(format!("{}: push failed - {}", repo.name, e))
                    }
                }
            }
        }
        println!();
    }

    // Preview mode: show what would be created
    if dry_run {
        Output::info(&format!("Branch: {}", branch));
        Output::info(&format!("Title: {}", pr_title));
        if let Some(pr_body) = body {
            Output::info(&format!("Body: {}", pr_body));
        }
        if draft {
            Output::info("Type: Draft PR");
        }
        println!();

        Output::subheader("Repositories that would create PRs:");
        for repo in &repos_with_changes {
            println!(
                "  - {} ({}/{}) → {}",
                repo.name,
                repo.owner,
                repo.repo,
                repo.target_branch()
            );
        }
        println!();
        Output::warning("Run without --dry-run to actually create the PRs.");
        return Ok(());
    }

    // Create PRs for each repo
    let mut created_prs: Vec<(String, u64, String)> = Vec::new(); // (repo_name, pr_number, url)
    let mut failed_repos: Vec<(String, String)> = Vec::new(); // (repo_name, error)

    for repo in &repos_with_changes {
        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        let spinner = Output::spinner(&format!("Creating PR for {}...", repo.name));

        match platform
            .create_pull_request(
                &repo.owner,
                &repo.repo,
                &branch,
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
                created_prs.push((repo.name.clone(), pr.number, pr.url.clone()));
            }
            Err(e) => {
                spinner.finish_with_message(format!("{}: failed - {}", repo.name, e));
                failed_repos.push((repo.name.clone(), e.to_string()));
            }
        }
    }

    // Save state
    if !created_prs.is_empty() {
        let state_path = workspace_root.join(".gitgrip").join("state.json");
        let mut state = if state_path.exists() {
            let content = std::fs::read_to_string(&state_path)?;
            StateFile::parse(&content).unwrap_or_default()
        } else {
            StateFile::default()
        };

        // Use the first PR number for branch mapping
        if let Some((_, first_pr_number, _)) = created_prs.first() {
            state.set_pr_for_branch(&branch, *first_pr_number);
        }

        let state_json = serde_json::to_string_pretty(&state)?;
        std::fs::write(&state_path, state_json)?;
    }

    // Summary
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
            number: u64,
            url: String,
        }
        #[derive(serde::Serialize)]
        struct JsonFailedRepo {
            repo: String,
            reason: String,
        }

        let result = JsonPrCreateResult {
            success: !created_prs.is_empty() && failed_repos.is_empty(),
            prs: created_prs
                .iter()
                .map(|(repo, number, url)| JsonCreatedPr {
                    repo: repo.clone(),
                    number: *number,
                    url: url.clone(),
                })
                .collect(),
            failed: failed_repos
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
        if created_prs.is_empty() && failed_repos.is_empty() {
            Output::warning("No PRs were created.");
        } else {
            if !created_prs.is_empty() {
                Output::success(&format!("Created {} PR(s):", created_prs.len()));
                for (repo_name, pr_number, url) in &created_prs {
                    println!("  {}: #{} - {}", repo_name, pr_number, url);
                }
            }
            if !failed_repos.is_empty() {
                if !created_prs.is_empty() {
                    println!();
                }
                Output::error(&format!("Failed to create {} PR(s):", failed_repos.len()));
                for (repo_name, error) in &failed_repos {
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

/// Check if a repo has changes ahead of its default branch
/// Returns Ok(true) if there are changes, Ok(false) if no changes or on default branch
pub(crate) fn check_repo_for_changes(
    repo: &RepoInfo,
    branch_name: &mut Option<String>,
) -> anyhow::Result<bool> {
    let git_repo = open_repo(&repo.absolute_path)
        .map_err(|e| anyhow::anyhow!("Failed to open repo: {}", e))?;

    let current = get_current_branch(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to get current branch: {}", e))?;

    // Skip if on target branch
    if current == repo.target_branch() {
        return Ok(false);
    }

    // Check for changes ahead of target branch
    let has_commits = has_commits_ahead(&git_repo, &current, repo.target_branch())
        .map_err(|e| anyhow::anyhow!("Failed to check commits: {}", e))?;

    // Also check for uncommitted changes (staged or unstaged)
    let has_uncommitted = has_uncommitted_changes(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to check uncommitted changes: {}", e))?;

    let has_changes = has_commits || has_uncommitted;

    if has_changes {
        // Update branch_name for consistency checking
        if branch_name.is_none() {
            *branch_name = Some(current);
        }
        Ok(true)
    } else {
        Ok(false)
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
}
