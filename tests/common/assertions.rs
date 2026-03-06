//! Custom assertion helpers for gitgrip integration tests.

use std::path::Path;

use super::git_helpers;

/// Assert that a repo is on the expected branch.
pub fn assert_on_branch(repo_path: &Path, expected: &str) {
    let actual = git_helpers::current_branch(repo_path);
    assert_eq!(
        actual,
        expected,
        "Expected repo at {} to be on branch '{}', but was on '{}'",
        repo_path.display(),
        expected,
        actual
    );
}

/// Assert that a local branch exists in the repo.
pub fn assert_branch_exists(repo_path: &Path, branch_name: &str) {
    assert!(
        git_helpers::branch_exists(repo_path, branch_name),
        "Expected branch '{}' to exist in {}",
        branch_name,
        repo_path.display()
    );
}

/// Assert that a local branch does NOT exist in the repo.
pub fn assert_branch_not_exists(repo_path: &Path, branch_name: &str) {
    assert!(
        !git_helpers::branch_exists(repo_path, branch_name),
        "Expected branch '{}' to NOT exist in {}",
        branch_name,
        repo_path.display()
    );
}

/// Assert that a file exists at the given path.
pub fn assert_file_exists(path: &Path) {
    assert!(path.exists(), "Expected file to exist: {}", path.display());
}

/// Assert the repo working tree is clean (no staged, modified, or untracked files).
pub fn assert_repo_clean(repo_path: &Path) {
    let output = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(repo_path)
        .output()
        .expect("failed to run git status");
    let status = String::from_utf8_lossy(&output.stdout);
    assert!(
        status.trim().is_empty(),
        "Expected repo at {} to be clean, but had:\n{}",
        repo_path.display(),
        status
    );
}

/// Assert that all repos in the workspace are on the given branch.
pub fn assert_all_on_branch(workspace_root: &Path, repo_names: &[String], branch: &str) {
    for name in repo_names {
        let repo_path = workspace_root.join(name);
        if repo_path.join(".git").exists() {
            assert_on_branch(&repo_path, branch);
        }
    }
}
