//! Git helper utilities for integration tests.
//!
//! Provides functions to create bare repos, commit files, create branches,
//! and push to remotes -- all using `git2` or `git` CLI for offline testing.

use std::fs;
use std::path::Path;
use std::process::Command;

/// Initialize a bare git repository at the given path.
/// Returns the path for convenience.
pub fn init_bare_repo(path: &Path) {
    fs::create_dir_all(path).unwrap();
    let status = Command::new("git")
        .args(["init", "--bare", "-b", "main"])
        .current_dir(path)
        .output()
        .expect("failed to init bare repo");
    assert!(
        status.status.success(),
        "git init --bare failed: {}",
        String::from_utf8_lossy(&status.stderr)
    );
}

/// Initialize a non-bare git repository with user config.
pub fn init_repo(path: &Path) {
    fs::create_dir_all(path).unwrap();
    git(path, &["init", "-b", "main"]);
    git(path, &["config", "user.email", "test@example.com"]);
    git(path, &["config", "user.name", "Test User"]);
}

/// Create a file, stage, and commit it. Returns the commit hash.
pub fn commit_file(repo_path: &Path, filename: &str, content: &str, message: &str) -> String {
    fs::write(repo_path.join(filename), content).unwrap();
    git(repo_path, &["add", filename]);
    git(repo_path, &["commit", "-m", message]);
    get_head_sha(repo_path)
}

/// Create and checkout a new branch.
pub fn create_branch(repo_path: &Path, branch_name: &str) {
    git(repo_path, &["checkout", "-b", branch_name]);
}

/// Checkout an existing branch.
pub fn checkout(repo_path: &Path, branch_name: &str) {
    git(repo_path, &["checkout", branch_name]);
}

/// Push a branch to a remote.
pub fn push_branch(repo_path: &Path, remote: &str, branch: &str) {
    git(repo_path, &["push", remote, branch]);
}

/// Fetch from a remote (optionally a single branch).
pub fn fetch(repo_path: &Path, remote: &str, branch: Option<&str>) {
    let mut args = vec!["fetch", remote];
    if let Some(branch) = branch {
        args.push(branch);
    }
    git(repo_path, &args);
}

/// Push with set-upstream.
pub fn push_upstream(repo_path: &Path, remote: &str, branch: &str) {
    git(repo_path, &["push", "-u", remote, branch]);
}

/// Add a remote to a repository.
pub fn add_remote(repo_path: &Path, name: &str, url: &str) {
    git(repo_path, &["remote", "add", name, url]);
}

/// Remove a remote from a repository.
pub fn remove_remote(repo_path: &Path, name: &str) {
    git(repo_path, &["remote", "remove", name]);
}

/// Get the current branch name.
pub fn current_branch(repo_path: &Path) -> String {
    git_output(repo_path, &["rev-parse", "--abbrev-ref", "HEAD"])
}

/// Get upstream tracking branch for a local branch.
pub fn branch_upstream(repo_path: &Path, branch_name: &str) -> Option<String> {
    let output = Command::new("git")
        .current_dir(repo_path)
        .args([
            "rev-parse",
            "--abbrev-ref",
            &format!("{}@{{upstream}}", branch_name),
        ])
        .output()
        .unwrap_or_else(|e| panic!("failed to run git rev-parse for upstream: {}", e));

    if !output.status.success() {
        return None;
    }

    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Check if recent log output contains a message.
pub fn log_contains(repo_path: &Path, message: &str) -> bool {
    git_output(repo_path, &["log", "--oneline", "-n", "10"]).contains(message)
}

/// Get HEAD sha.
pub fn get_head_sha(repo_path: &Path) -> String {
    git_output(repo_path, &["rev-parse", "HEAD"])
}

/// Check if a local branch exists.
pub fn branch_exists(repo_path: &Path, branch_name: &str) -> bool {
    Command::new("git")
        .args([
            "rev-parse",
            "--verify",
            &format!("refs/heads/{}", branch_name),
        ])
        .current_dir(repo_path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Clone a repository from a URL (typically file://).
pub fn clone_repo(url: &str, dest: &Path) {
    let status = Command::new("git")
        .args(["clone", url, dest.to_str().unwrap()])
        .output()
        .expect("failed to clone repo");
    assert!(
        status.status.success(),
        "git clone failed: {}",
        String::from_utf8_lossy(&status.stderr)
    );
    configure_identity(dest);
}

/// Configure local git identity for commits in a repo.
pub fn configure_identity(repo_path: &Path) {
    git(repo_path, &["config", "user.email", "test@example.com"]);
    git(repo_path, &["config", "user.name", "Test User"]);
}

/// Run a git command, panic on failure.
fn git(dir: &Path, args: &[&str]) {
    let output = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .unwrap_or_else(|e| panic!("failed to run git {:?}: {}", args, e));
    assert!(
        output.status.success(),
        "git {:?} failed in {}: {}",
        args,
        dir.display(),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Run a git command and return trimmed stdout.
fn git_output(dir: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .unwrap_or_else(|e| panic!("failed to run git {:?}: {}", args, e));
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}
