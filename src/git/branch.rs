//! Git branch operations

use git2::Repository;
use std::process::Command;

use super::{get_current_branch, GitError};
use crate::util::log_cmd;

/// Create a new local branch and check it out
pub fn create_and_checkout_branch(repo: &Repository, branch_name: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["checkout", "-b", branch_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);

        // Detect worktree conflict and provide helpful message
        if stderr.contains("is already used by worktree at") {
            // Extract worktree path from error message
            if let Some(path_start) = stderr.find("worktree at '") {
                let path_part = &stderr[path_start + 13..];
                if let Some(path_end) = path_part.find('\'') {
                    let worktree_path = &path_part[..path_end];
                    return Err(GitError::OperationFailed(format!(
                        "Branch '{}' is checked out in another worktree at '{}'. \
                         Use a different branch name or work in that worktree.",
                        branch_name, worktree_path
                    )));
                }
            }
            return Err(GitError::OperationFailed(format!(
                "Branch '{}' is already checked out in another worktree. \
                 Use a different branch name or work in that worktree.",
                branch_name
            )));
        }

        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Checkout an existing branch
pub fn checkout_branch(repo: &Repository, branch_name: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    // Check if branch exists
    if !branch_exists(repo, branch_name) {
        return Err(GitError::BranchNotFound(branch_name.to_string()));
    }

    let mut cmd = Command::new("git");
    cmd.args(["checkout", branch_name]).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);

        // Detect worktree conflict and provide helpful message
        if stderr.contains("is already used by worktree at") {
            // Extract worktree path from error message
            if let Some(path_start) = stderr.find("worktree at '") {
                let path_part = &stderr[path_start + 13..];
                if let Some(path_end) = path_part.find('\'') {
                    let worktree_path = &path_part[..path_end];
                    return Err(GitError::OperationFailed(format!(
                        "Branch '{}' is checked out in another worktree at '{}'. \
                         Either use that worktree or create a new branch with 'gr branch <name>'",
                        branch_name, worktree_path
                    )));
                }
            }
            return Err(GitError::OperationFailed(format!(
                "Branch '{}' is already checked out in another worktree. \
                 Either use that worktree or create a new branch with 'gr branch <name>'",
                branch_name
            )));
        }

        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Checkout or reset a local branch to a specific upstream ref.
///
/// Uses `git checkout -B <branch> <upstream>` to ensure the local branch
/// points at the upstream commit.
pub fn checkout_branch_at_upstream(
    repo: &Repository,
    branch_name: &str,
    upstream: &str,
) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["checkout", "-B", branch_name, upstream])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);

        if stderr.contains("is already used by worktree at") {
            if let Some(path_start) = stderr.find("worktree at '") {
                let path_part = &stderr[path_start + 13..];
                if let Some(path_end) = path_part.find('\'') {
                    let worktree_path = &path_part[..path_end];
                    return Err(GitError::OperationFailed(format!(
                        "Branch '{}' is checked out in another worktree at '{}'. \
                         Use that worktree or choose a different branch.",
                        branch_name, worktree_path
                    )));
                }
            }
            return Err(GitError::OperationFailed(format!(
                "Branch '{}' is already checked out in another worktree. \
                 Use that worktree or choose a different branch.",
                branch_name
            )));
        }

        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Checkout a target in detached HEAD mode.
///
/// Useful when the corresponding local branch is locked in another worktree.
pub fn checkout_detached(repo: &Repository, target: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["checkout", "--detach", "-f", target])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Check if a local branch exists
pub fn branch_exists(repo: &Repository, branch_name: &str) -> bool {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args([
        "rev-parse",
        "--verify",
        &format!("refs/heads/{}", branch_name),
    ])
    .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output();

    output.map(|o| o.status.success()).unwrap_or(false)
}

/// Check if a remote branch exists
pub fn remote_branch_exists(repo: &Repository, branch_name: &str, remote: &str) -> bool {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args([
        "rev-parse",
        "--verify",
        &format!("refs/remotes/{}/{}", remote, branch_name),
    ])
    .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output();

    output.map(|o| o.status.success()).unwrap_or(false)
}

/// Delete a local branch
pub fn delete_local_branch(
    repo: &Repository,
    branch_name: &str,
    force: bool,
) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    // Check if it's the current branch
    let current = get_current_branch(repo)?;
    if current == branch_name {
        return Err(GitError::OperationFailed(
            "Cannot delete the currently checked out branch".to_string(),
        ));
    }

    let flag = if force { "-D" } else { "-d" };
    let mut cmd = Command::new("git");
    cmd.args(["branch", flag, branch_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("not fully merged") {
            return Err(GitError::OperationFailed(format!(
                "Branch '{}' is not fully merged. Use force to delete anyway.",
                branch_name
            )));
        }
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Check if a branch has been merged into another branch
pub fn is_branch_merged(
    repo: &Repository,
    branch_name: &str,
    target_branch: &str,
) -> Result<bool, GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["branch", "--merged", target_branch])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout
        .lines()
        .any(|line| line.trim().trim_start_matches("* ") == branch_name))
}

/// Get list of local branches
pub fn list_local_branches(repo: &Repository) -> Result<Vec<String>, GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["branch", "--format=%(refname:short)"])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout.lines().map(|s| s.to_string()).collect())
}

/// Get list of remote branches
pub fn list_remote_branches(repo: &Repository, remote: &str) -> Result<Vec<String>, GitError> {
    let repo_path = super::get_workdir(repo);
    let prefix = format!("{}/", remote);

    let mut cmd = Command::new("git");
    cmd.args(["branch", "-r", "--format=%(refname:short)"])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout
        .lines()
        .filter(|line| line.starts_with(&prefix))
        .map(|line| line[prefix.len()..].to_string())
        .collect())
}

/// Get commits between current branch and base branch
pub fn get_commits_between(
    repo: &Repository,
    base_branch: &str,
    head_branch: Option<&str>,
) -> Result<Vec<String>, GitError> {
    let repo_path = super::get_workdir(repo);

    let head_name = match head_branch {
        Some(name) => name.to_string(),
        None => get_current_branch(repo)?,
    };

    let range = format!("{}..{}", base_branch, head_name);
    let mut cmd = Command::new("git");
    cmd.args(["rev-list", &range]).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout.lines().map(|s| s.to_string()).collect())
}

/// Check if branch has commits not in base
pub fn has_commits_ahead(repo: &Repository, base_branch: &str) -> Result<bool, GitError> {
    let commits = get_commits_between(repo, base_branch, None)?;
    Ok(!commits.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::git::open_repo;
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

        // Create initial commit
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

        let repo = open_repo(temp.path()).unwrap();
        (temp, repo)
    }

    #[test]
    fn test_create_and_checkout_branch() {
        let (temp, repo) = setup_test_repo();

        create_and_checkout_branch(&repo, "feature").unwrap();

        let current = get_current_branch(&repo).unwrap();
        assert_eq!(current, "feature");
    }

    #[test]
    fn test_branch_exists() {
        let (temp, repo) = setup_test_repo();

        assert!(!branch_exists(&repo, "feature"));

        create_and_checkout_branch(&repo, "feature").unwrap();
        assert!(branch_exists(&repo, "feature"));
    }

    #[test]
    fn test_checkout_branch() {
        let (temp, repo) = setup_test_repo();

        // Create a feature branch
        create_and_checkout_branch(&repo, "feature").unwrap();

        // Go back to main/master
        let default = if branch_exists(&repo, "main") {
            "main"
        } else {
            "master"
        };
        checkout_branch(&repo, default).unwrap();

        let current = get_current_branch(&repo).unwrap();
        assert_eq!(current, default);
    }

    #[test]
    fn test_list_local_branches() {
        let (temp, repo) = setup_test_repo();

        create_and_checkout_branch(&repo, "feature1").unwrap();
        create_and_checkout_branch(&repo, "feature2").unwrap();

        let branches = list_local_branches(&repo).unwrap();
        assert!(branches.contains(&"feature1".to_string()));
        assert!(branches.contains(&"feature2".to_string()));
    }

    #[test]
    fn test_checkout_nonexistent_branch() {
        let (_temp, repo) = setup_test_repo();
        let result = checkout_branch(&repo, "nonexistent");
        assert!(result.is_err());
        match result.unwrap_err() {
            GitError::BranchNotFound(name) => assert_eq!(name, "nonexistent"),
            e => panic!("Expected BranchNotFound, got: {:?}", e),
        }
    }

    #[test]
    fn test_delete_local_branch() {
        let (_temp, repo) = setup_test_repo();

        // Create a branch, go back to default
        create_and_checkout_branch(&repo, "to-delete").unwrap();
        let default = if branch_exists(&repo, "main") {
            "main"
        } else {
            "master"
        };
        checkout_branch(&repo, default).unwrap();

        // Delete the branch
        delete_local_branch(&repo, "to-delete", false).unwrap();
        assert!(!branch_exists(&repo, "to-delete"));
    }

    #[test]
    fn test_delete_current_branch_fails() {
        let (_temp, repo) = setup_test_repo();
        create_and_checkout_branch(&repo, "current").unwrap();

        let result = delete_local_branch(&repo, "current", false);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("currently checked out"));
    }

    #[test]
    fn test_is_branch_merged() {
        let (temp, repo) = setup_test_repo();
        let default = if branch_exists(&repo, "main") {
            "main"
        } else {
            "master"
        };

        // Create a branch, add a commit, merge it back
        create_and_checkout_branch(&repo, "to-merge").unwrap();
        fs::write(temp.path().join("new-file.txt"), "content").unwrap();
        Command::new("git")
            .args(["add", "new-file.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "new commit"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        checkout_branch(&repo, default).unwrap();
        Command::new("git")
            .args(["merge", "to-merge"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        assert!(is_branch_merged(&repo, "to-merge", default).unwrap());
    }

    #[test]
    fn test_has_commits_ahead() {
        let (temp, repo) = setup_test_repo();
        let default = if branch_exists(&repo, "main") {
            "main"
        } else {
            "master"
        };

        // On default branch, no commits ahead of itself
        assert!(!has_commits_ahead(&repo, default).unwrap());

        // Create a branch with a commit
        create_and_checkout_branch(&repo, "ahead").unwrap();
        fs::write(temp.path().join("ahead.txt"), "ahead content").unwrap();
        Command::new("git")
            .args(["add", "ahead.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "ahead commit"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        assert!(has_commits_ahead(&repo, default).unwrap());
    }

    #[test]
    fn test_get_commits_between() {
        let (temp, repo) = setup_test_repo();
        let default = if branch_exists(&repo, "main") {
            "main"
        } else {
            "master"
        };

        create_and_checkout_branch(&repo, "multi-commit").unwrap();
        for i in 0..3 {
            fs::write(temp.path().join(format!("file{}.txt", i)), format!("content {}", i)).unwrap();
            Command::new("git")
                .args(["add", "."])
                .current_dir(temp.path())
                .output()
                .unwrap();
            Command::new("git")
                .args(["commit", "-m", &format!("commit {}", i)])
                .current_dir(temp.path())
                .output()
                .unwrap();
        }

        let commits = get_commits_between(&repo, default, Some("multi-commit")).unwrap();
        assert_eq!(commits.len(), 3);
    }

    #[test]
    fn test_remote_branch_exists() {
        let (_temp, repo) = setup_test_repo();
        // No remote set up, so remote branch should not exist
        assert!(!remote_branch_exists(&repo, "main", "origin"));
    }
}
