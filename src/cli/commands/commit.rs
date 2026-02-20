//! Commit command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::RepoInfo;
use crate::git::cache::invalidate_status_cache;
use crate::git::{get_workdir, open_repo, path_exists};
use crate::util::log_cmd;
use git2::Repository;
use std::path::PathBuf;
use std::process::Command;

/// Run the commit command
pub fn run_commit(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    message: &str,
    amend: bool,
    json: bool,
) -> anyhow::Result<()> {
    if !json {
        Output::header("Committing changes...");
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
        .collect();

    let mut success_count = 0;
    let mut skip_count = 0;

    #[derive(serde::Serialize)]
    struct JsonCommit {
        repo: String,
        sha: String,
    }
    let mut json_committed: Vec<JsonCommit> = Vec::new();
    let mut json_skipped: Vec<String> = Vec::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                // Check if there are staged changes
                if !has_staged_changes(&git_repo)? {
                    skip_count += 1;
                    json_skipped.push(repo.name.clone());
                    continue;
                }

                match create_commit(&git_repo, message, amend) {
                    Ok(commit_id) => {
                        let short_id = &commit_id[..7.min(commit_id.len())];
                        if !json {
                            if amend {
                                Output::success(&format!("{}: amended ({})", repo.name, short_id));
                            } else {
                                Output::success(&format!(
                                    "{}: committed ({})",
                                    repo.name, short_id
                                ));
                            }
                        }
                        success_count += 1;
                        json_committed.push(JsonCommit {
                            repo: repo.name.clone(),
                            sha: commit_id.clone(),
                        });
                        invalidate_status_cache(&repo.absolute_path);
                    }
                    Err(e) => {
                        if !json {
                            Output::error(&format!("{}: {}", repo.name, e));
                        }
                    }
                }
            }
            Err(e) => {
                if !json {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
            }
        }
    }

    // Also handle manifest worktree if it exists (in griptree scenario)
    if let Some(manifests_dir) = manifest_paths::resolve_manifest_repo_dir(workspace_root) {
        let manifests_git_dir = manifests_dir.join(".git");
        if manifests_git_dir.exists() && path_exists(&manifests_dir) {
            match open_repo(&manifests_dir) {
                Ok(git_repo) => {
                    if has_staged_changes(&git_repo)? {
                        match create_commit(&git_repo, message, amend) {
                            Ok(commit_id) => {
                                let short_id = &commit_id[..7.min(commit_id.len())];
                                if !json {
                                    if amend {
                                        Output::success(&format!(
                                            "manifest: amended ({})",
                                            short_id
                                        ));
                                    } else {
                                        Output::success(&format!(
                                            "manifest: committed ({})",
                                            short_id
                                        ));
                                    }
                                }
                                success_count += 1;
                                json_committed.push(JsonCommit {
                                    repo: "manifest".to_string(),
                                    sha: commit_id.clone(),
                                });
                                invalidate_status_cache(&manifests_dir);
                            }
                            Err(e) => {
                                if !json {
                                    Output::error(&format!("manifest: {}", e));
                                }
                            }
                        }
                    }
                }
                Err(e) => {
                    if !json {
                        Output::warning(&format!("manifest: {}", e));
                    }
                }
            }
        }
    }

    if json {
        #[derive(serde::Serialize)]
        struct JsonCommitResult {
            success: bool,
            committed: Vec<JsonCommit>,
            skipped: Vec<String>,
        }

        let result = JsonCommitResult {
            success: !json_committed.is_empty(),
            committed: json_committed,
            skipped: json_skipped,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if success_count > 0 {
            println!(
                "Created {} commit(s){}.",
                success_count,
                if skip_count > 0 {
                    format!(", {} repo(s) had no staged changes", skip_count)
                } else {
                    String::new()
                }
            );
        } else {
            println!("No changes to commit.");
        }
    }

    Ok(())
}

/// Check if a repository has staged changes using git CLI
fn has_staged_changes(repo: &Repository) -> anyhow::Result<bool> {
    let repo_path = get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["diff", "--cached", "--quiet"])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output()?;

    // Exit code 0 means no diff (no staged changes)
    // Exit code 1 means there are changes
    Ok(!output.status.success())
}

/// Create a commit in the repository using git CLI
fn create_commit(repo: &Repository, message: &str, amend: bool) -> anyhow::Result<String> {
    let repo_path = get_workdir(repo);

    let mut args = vec!["commit", "-m", message];
    if amend {
        args.push("--amend");
    }

    let mut cmd = Command::new("git");
    cmd.args(&args).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output()?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("git commit failed: {}", stderr);
    }

    // Get the commit hash
    let mut cmd = Command::new("git");
    cmd.args(["rev-parse", "HEAD"]).current_dir(repo_path);
    log_cmd(&cmd);
    let hash_output = cmd.output()?;

    let commit_id = String::from_utf8_lossy(&hash_output.stdout)
        .trim()
        .to_string();
    Ok(commit_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::git::open_repo;
    use std::fs;
    use std::process::Command as StdCommand;
    use tempfile::TempDir;

    fn setup_test_repo() -> (TempDir, Repository) {
        let temp_dir = TempDir::new().unwrap();

        StdCommand::new("git")
            .args(["init"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        StdCommand::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        StdCommand::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        let repo = open_repo(temp_dir.path()).unwrap();
        (temp_dir, repo)
    }

    #[test]
    fn test_has_staged_changes_empty() {
        let (_temp_dir, repo) = setup_test_repo();
        assert!(!has_staged_changes(&repo).unwrap());
    }

    #[test]
    fn test_has_staged_changes_with_staged() {
        let (temp_dir, repo) = setup_test_repo();

        // Create and stage a file
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "content").unwrap();

        StdCommand::new("git")
            .args(["add", "test.txt"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        assert!(has_staged_changes(&repo).unwrap());
    }

    #[test]
    fn test_create_commit() {
        let (temp_dir, repo) = setup_test_repo();

        // Create and stage a file
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "content").unwrap();

        StdCommand::new("git")
            .args(["add", "test.txt"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        let commit_id = create_commit(&repo, "Test commit", false).unwrap();
        assert!(!commit_id.is_empty());

        // Verify commit was created
        let output = StdCommand::new("git")
            .args(["log", "-1", "--format=%s"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();
        let message = String::from_utf8_lossy(&output.stdout).trim().to_string();
        assert_eq!(message, "Test commit");
    }

    #[test]
    fn test_amend_commit() {
        let (temp_dir, repo) = setup_test_repo();

        // Create initial commit
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "initial").unwrap();

        StdCommand::new("git")
            .args(["add", "test.txt"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        create_commit(&repo, "Initial commit", false).unwrap();

        // Modify and stage
        fs::write(&file_path, "amended").unwrap();

        StdCommand::new("git")
            .args(["add", "test.txt"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();

        // Amend
        create_commit(&repo, "Amended commit", true).unwrap();

        // Verify only one commit exists
        let output = StdCommand::new("git")
            .args(["rev-list", "--count", "HEAD"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();
        let count: usize = String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse()
            .unwrap();
        assert_eq!(count, 1);

        // Verify message was updated
        let output = StdCommand::new("git")
            .args(["log", "-1", "--format=%s"])
            .current_dir(temp_dir.path())
            .output()
            .unwrap();
        let message = String::from_utf8_lossy(&output.stdout).trim().to_string();
        assert_eq!(message, "Amended commit");
    }
}
