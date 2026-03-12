//! Push command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::git::remote::{force_push_branch, push_branch};
use crate::git::{get_current_branch, open_repo, path_exists};
use git2::Repository;
use std::path::Path;

/// JSON-serializable push error for --json output
#[derive(serde::Serialize)]
struct JsonPushError {
    repo: String,
    reason: String,
}

/// Run the push command
#[allow(clippy::too_many_arguments)]
pub fn run_push(
    workspace_root: &Path,
    manifest: &Manifest,
    set_upstream: bool,
    force: bool,
    quiet: bool,
    json: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    if !json {
        if force {
            Output::header("Force pushing changes...");
        } else {
            Output::header("Pushing changes...");
        }
        println!();
    }

    let mut repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    // Include manifest repo, respecting --repo filter
    let include_manifest = match repos_filter {
        None => true,
        Some(filter) => filter.iter().any(|r| r == "manifest"),
    };
    if include_manifest {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            repos.push(manifest_repo);
        }
    }

    let mut success_count = 0;
    let mut skip_count = 0;
    let mut error_count = 0;
    let mut failed_repos: Vec<(String, String)> = Vec::new(); // (repo_name, error_message)
    let mut json_pushed: Vec<String> = Vec::new();
    let mut json_skipped: Vec<String> = Vec::new();
    let mut json_failed: Vec<JsonPushError> = Vec::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            skip_count += 1;
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let branch = match get_current_branch(&git_repo) {
                    Ok(b) => b,
                    Err(e) => {
                        if !json {
                            Output::error(&format!("{}: {}", repo.name, e));
                        }
                        error_count += 1;
                        json_failed.push(JsonPushError {
                            repo: repo.name.clone(),
                            reason: e.to_string(),
                        });
                        continue;
                    }
                };

                // Check if there's anything to push
                if !has_commits_to_push(&git_repo, &branch, repo.target_branch())? {
                    if !quiet && !json {
                        Output::info(&format!("{}: nothing to push", repo.name));
                    }
                    skip_count += 1;
                    json_skipped.push(repo.name.clone());
                    continue;
                }

                let action = if force { "Force pushing" } else { "Pushing" };
                let spinner = if !json {
                    Some(Output::spinner(&format!("{} {}...", action, repo.name)))
                } else {
                    None
                };

                let result = if force {
                    force_push_branch(&git_repo, &branch, &repo.push_remote)
                } else {
                    push_branch(&git_repo, &branch, &repo.push_remote, set_upstream)
                };

                match result {
                    Ok(()) => {
                        if let Some(s) = spinner {
                            let msg = if force {
                                format!("{}: force pushed", repo.name)
                            } else if set_upstream {
                                format!("{}: pushed and set upstream", repo.name)
                            } else {
                                format!("{}: pushed", repo.name)
                            };
                            s.finish_with_message(msg);
                        }
                        success_count += 1;
                        json_pushed.push(repo.name.clone());
                    }
                    Err(e) => {
                        // Check if this is a "nothing to push" situation
                        let error_msg = e.to_string().to_lowercase();
                        if error_msg.contains("everything up-to-date")
                            || error_msg.contains("nothing to commit")
                            || error_msg.contains("nothing to push")
                            || error_msg.contains("no changes")
                            || error_msg.contains("already up to date")
                        {
                            if let Some(s) = spinner {
                                if !quiet {
                                    s.finish_with_message(format!(
                                        "{}: skipped (nothing to push)",
                                        repo.name
                                    ));
                                } else {
                                    s.finish_and_clear();
                                }
                            }
                            skip_count += 1;
                            json_skipped.push(repo.name.clone());
                        } else {
                            if let Some(s) = spinner {
                                s.finish_with_message(format!("{}: failed - {}", repo.name, e));
                            }
                            error_count += 1;
                            failed_repos.push((repo.name.clone(), format!("Error: {}", e)));
                            json_failed.push(JsonPushError {
                                repo: repo.name.clone(),
                                reason: e.to_string(),
                            });
                        }
                    }
                }
            }
            Err(e) => {
                if !json {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
                error_count += 1;
                failed_repos.push((repo.name.clone(), format!("Error: {}", e)));
                json_failed.push(JsonPushError {
                    repo: repo.name.clone(),
                    reason: e.to_string(),
                });
            }
        }
    }

    if json {
        #[derive(serde::Serialize)]
        struct JsonPushResult {
            success: bool,
            pushed: Vec<String>,
            skipped: Vec<String>,
            failed: Vec<JsonPushError>,
        }

        let result = JsonPushResult {
            success: error_count == 0,
            pushed: json_pushed,
            skipped: json_skipped,
            failed: json_failed,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        let action = if force { "Force pushed" } else { "Pushed" };
        if error_count == 0 {
            if success_count > 0 {
                Output::success(&format!(
                    "{} {} repo(s){}.",
                    action,
                    success_count,
                    if skip_count > 0 {
                        format!(", {} had nothing to push", skip_count)
                    } else {
                        String::new()
                    }
                ));
            } else {
                println!("Nothing to push.");
            }
        } else {
            Output::warning(&format!(
                "{} {}, {} failed, {} skipped",
                success_count,
                action.to_lowercase(),
                error_count,
                skip_count
            ));

            // Show which repos failed and why
            if !failed_repos.is_empty() {
                println!();
                for (repo_name, error_msg) in &failed_repos {
                    println!("  ✗ {}: {}", repo_name, error_msg);
                }
            }
        }
    }

    Ok(())
}

/// Check if branch has commits that aren't on the remote
fn has_commits_to_push(
    repo: &Repository,
    branch: &str,
    default_branch: &str,
) -> anyhow::Result<bool> {
    // Try to find the remote tracking branch
    let remote_ref = format!("refs/remotes/origin/{}", branch);

    let local_ref = match repo.find_reference(&format!("refs/heads/{}", branch)) {
        Ok(r) => r,
        Err(_) => return Ok(false),
    };

    let local_oid = local_ref.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve local branch '{}'. Ensure it exists and has at least one commit.",
            branch
        )
    })?;

    let remote_branch = match repo.find_reference(&remote_ref) {
        Ok(r) => r,
        Err(_) => {
            // No remote tracking branch for this branch.
            // If we're on the default branch itself, this is likely the first push
            // — always allow it.
            if branch == default_branch {
                return Ok(local_ref.peel_to_commit().is_ok());
            }

            // For feature branches, compare against origin/<default_branch>
            // to check for unique commits (not just "any commits exist").
            let base_ref = format!("refs/remotes/origin/{}", default_branch);
            let base = match repo.find_reference(&base_ref) {
                Ok(r) => r,
                Err(_) => {
                    // No remote default branch either — fall back to local default branch
                    match repo.find_reference(&format!("refs/heads/{}", default_branch)) {
                        Ok(r) => r,
                        // Can't determine base; assume there are commits to push
                        Err(_) => return Ok(true),
                    }
                }
            };

            let base_oid = match base.target() {
                Some(oid) => oid,
                None => return Ok(true),
            };

            if local_oid == base_oid {
                return Ok(false);
            }

            let (ahead, _behind) = repo.graph_ahead_behind(local_oid, base_oid)?;
            return Ok(ahead > 0);
        }
    };

    let remote_oid = remote_branch.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve remote tracking branch 'origin/{}'. Try running `gr sync` first.",
            branch
        )
    })?;

    // If they're the same, nothing to push
    if local_oid == remote_oid {
        return Ok(false);
    }

    // Check if local is ahead of remote
    let (ahead, _behind) = repo.graph_ahead_behind(local_oid, remote_oid)?;
    Ok(ahead > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn setup_test_repo() -> (TempDir, Repository) {
        let temp_dir = TempDir::new().unwrap();
        let repo = Repository::init(temp_dir.path()).unwrap();

        // Configure user for commits
        let mut config = repo.config().unwrap();
        config.set_str("user.name", "Test User").unwrap();
        config.set_str("user.email", "test@example.com").unwrap();

        // Create initial commit
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "content").unwrap();

        {
            let mut index = repo.index().unwrap();
            index.add_path(std::path::Path::new("test.txt")).unwrap();
            index.write().unwrap();

            let tree_id = index.write_tree().unwrap();
            let tree = repo.find_tree(tree_id).unwrap();
            let sig = repo.signature().unwrap();

            repo.commit(Some("HEAD"), &sig, &sig, "Initial commit", &tree, &[])
                .unwrap();
        }

        (temp_dir, repo)
    }

    #[test]
    fn test_has_commits_to_push_no_remote() {
        let (_temp_dir, repo) = setup_test_repo();

        // On the default branch with no remote — should return true
        // (this is the initial push scenario)
        let result = has_commits_to_push(&repo, "master", "master").unwrap();
        assert!(result);
    }

    #[test]
    fn test_has_commits_to_push_new_branch_no_changes() {
        let (temp_dir, repo) = setup_test_repo();

        // Get the default branch name
        let head = repo.head().unwrap();
        let default_branch = head.shorthand().unwrap().to_string();

        // Create a new branch from the same commit (no new commits)
        let head_commit = head.peel_to_commit().unwrap();
        repo.branch("feat/test", &head_commit, false).unwrap();

        // The new branch has no unique commits vs the default branch
        let result = has_commits_to_push(&repo, "feat/test", &default_branch).unwrap();
        assert!(!result, "Should not push branch with no unique commits");

        let _ = temp_dir; // keep alive
    }

    #[test]
    fn test_has_commits_to_push_new_branch_with_changes() {
        let (temp_dir, repo) = setup_test_repo();

        let head = repo.head().unwrap();
        let default_branch = head.shorthand().unwrap().to_string();
        let head_commit = head.peel_to_commit().unwrap();

        // Create a new branch and add a commit
        repo.branch("feat/test", &head_commit, false).unwrap();
        repo.set_head("refs/heads/feat/test").unwrap();

        let file_path = temp_dir.path().join("feature.txt");
        fs::write(&file_path, "new feature").unwrap();
        let mut index = repo.index().unwrap();
        index.add_path(std::path::Path::new("feature.txt")).unwrap();
        index.write().unwrap();
        let tree_id = index.write_tree().unwrap();
        let tree = repo.find_tree(tree_id).unwrap();
        let sig = repo.signature().unwrap();
        repo.commit(
            Some("HEAD"),
            &sig,
            &sig,
            "Add feature",
            &tree,
            &[&head_commit],
        )
        .unwrap();

        let result = has_commits_to_push(&repo, "feat/test", &default_branch).unwrap();
        assert!(result, "Should push branch with unique commits");
    }
}
