//! Git status operations

use git2::Repository;
use std::path::PathBuf;
use std::process::Command;

use std::path::Path;

use super::cache::STATUS_CACHE;
use super::{get_current_branch, open_repo, path_exists, GitError};
use crate::core::repo::RepoInfo;
use crate::util::log_cmd;

/// State of an in-progress git operation
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RepositoryState {
    Clean,
    Rebasing,
    Merging,
    CherryPicking,
}

impl RepositoryState {
    /// Detect in-progress git operations by checking sentinel files in .git/
    pub fn detect(repo_path: &Path) -> Self {
        // For worktrees, .git may be a file pointing to the real gitdir.
        // Use `git rev-parse --git-dir` to resolve the actual git directory.
        let git_dir = resolve_git_dir(repo_path);

        if git_dir.join("rebase-merge").exists() || git_dir.join("rebase-apply").exists() {
            RepositoryState::Rebasing
        } else if git_dir.join("MERGE_HEAD").exists() {
            RepositoryState::Merging
        } else if git_dir.join("CHERRY_PICK_HEAD").exists() {
            RepositoryState::CherryPicking
        } else {
            RepositoryState::Clean
        }
    }

    pub fn label(&self) -> Option<&'static str> {
        match self {
            RepositoryState::Clean => None,
            RepositoryState::Rebasing => Some("REBASING"),
            RepositoryState::Merging => Some("MERGING"),
            RepositoryState::CherryPicking => Some("CHERRY-PICKING"),
        }
    }
}

/// Resolve the actual .git directory (handles worktrees where .git is a file)
fn resolve_git_dir(repo_path: &Path) -> PathBuf {
    let dot_git = repo_path.join(".git");
    if dot_git.is_dir() {
        return dot_git;
    }
    // Worktree: .git is a file containing "gitdir: /path/to/real/gitdir"
    if dot_git.is_file() {
        if let Ok(content) = std::fs::read_to_string(&dot_git) {
            if let Some(gitdir) = content.strip_prefix("gitdir: ") {
                let gitdir = gitdir.trim();
                let path = PathBuf::from(gitdir);
                if path.is_absolute() {
                    return path;
                }
                // Relative path — resolve from repo_path
                return repo_path.join(path);
            }
        }
    }
    dot_git
}

/// Repository status information
#[derive(Debug, Clone)]
pub struct RepoStatusInfo {
    /// Current branch name
    pub current_branch: String,
    /// Is the working directory clean
    pub is_clean: bool,
    /// Staged files
    pub staged: Vec<String>,
    /// Modified files (not staged)
    pub modified: Vec<String>,
    /// Untracked files
    pub untracked: Vec<String>,
    /// Commits ahead of remote
    pub ahead: usize,
    /// Commits behind remote
    pub behind: usize,
}

/// Repository status with name
#[derive(Debug, Clone)]
pub struct RepoStatus {
    /// Repository name
    pub name: String,
    /// Current branch
    pub branch: String,
    /// Is clean
    pub clean: bool,
    /// Staged file count
    pub staged: usize,
    /// Modified file count
    pub modified: usize,
    /// Untracked file count
    pub untracked: usize,
    /// Commits ahead of upstream
    pub ahead: usize,
    /// Commits behind upstream
    pub behind: usize,
    /// Commits ahead of default branch (main)
    pub ahead_main: usize,
    /// Commits behind default branch (main)
    pub behind_main: usize,
    /// Whether repo exists
    pub exists: bool,
    /// In-progress git operation (rebase, merge, cherry-pick)
    pub state: RepositoryState,
}

/// Get detailed status for a repository using git2
pub fn get_status_info(repo: &Repository) -> Result<RepoStatusInfo, GitError> {
    let current_branch = get_current_branch(repo)?;

    // Use git porcelain status for reliable parsing
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["status", "--porcelain=v1"])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let stdout = String::from_utf8_lossy(&output.stdout);

    let mut staged = Vec::new();
    let mut modified = Vec::new();
    let mut untracked = Vec::new();

    for line in stdout.lines() {
        if line.len() < 3 {
            continue;
        }
        let index_status = line.chars().next().unwrap_or(' ');
        let worktree_status = line.chars().nth(1).unwrap_or(' ');
        let path = line[3..].to_string();

        // Staged changes (index)
        if matches!(index_status, 'A' | 'M' | 'D' | 'R' | 'C' | 'T') {
            staged.push(path.clone());
        }

        // Worktree changes
        if matches!(worktree_status, 'M' | 'D' | 'T') {
            modified.push(path.clone());
        }

        // Untracked
        if index_status == '?' && worktree_status == '?' {
            untracked.push(path);
        }
    }

    let is_clean = staged.is_empty() && modified.is_empty() && untracked.is_empty();

    // Get ahead/behind counts
    let (ahead, behind) = get_ahead_behind_git(repo_path).unwrap_or((0, 0));

    Ok(RepoStatusInfo {
        current_branch,
        is_clean,
        staged,
        modified,
        untracked,
        ahead,
        behind,
    })
}

/// Get cached status or compute it
pub fn get_cached_status(repo_path: &PathBuf) -> Result<RepoStatusInfo, GitError> {
    // Check cache first
    if let Some(status) = STATUS_CACHE.get(repo_path) {
        return Ok(status);
    }

    // Compute and cache
    let repo = open_repo(repo_path)?;
    let status = get_status_info(&repo)?;
    STATUS_CACHE.set(repo_path.clone(), status.clone());
    Ok(status)
}

/// Get ahead/behind counts using git rev-list
fn get_ahead_behind_git(repo_path: &std::path::Path) -> Option<(usize, usize)> {
    let mut cmd = Command::new("git");
    cmd.args(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output().ok()?;

    if !output.status.success() {
        return Some((0, 0));
    }

    parse_ahead_behind(&output.stdout)
}

/// Get commits ahead/behind a specific branch (e.g., main)
fn get_ahead_behind_branch(
    repo_path: &std::path::Path,
    base_branch: &str,
) -> Option<(usize, usize)> {
    // Try remote first: origin/{base_branch}
    let remote_ref = format!("origin/{}", base_branch);

    let mut cmd = Command::new("git");
    cmd.args([
        "rev-list",
        "--left-right",
        "--count",
        &format!("{}...HEAD", remote_ref),
    ])
    .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd.output().ok()?;

    if !output.status.success() {
        // Fallback to local branch
        let mut cmd = Command::new("git");
        cmd.args([
            "rev-list",
            "--left-right",
            "--count",
            &format!("{}...HEAD", base_branch),
        ])
        .current_dir(repo_path);
        log_cmd(&cmd);
        let output = cmd.output().ok()?;

        if !output.status.success() {
            return Some((0, 0));
        }
        return parse_ahead_behind(&output.stdout);
    }

    parse_ahead_behind(&output.stdout)
}

/// Parse ahead/behind counts from git rev-list output
fn parse_ahead_behind(stdout: &[u8]) -> Option<(usize, usize)> {
    let stdout = String::from_utf8_lossy(stdout);
    let parts: Vec<&str> = stdout.trim().split('\t').collect();

    if parts.len() == 2 {
        let behind = parts[0].parse().unwrap_or(0);
        let ahead = parts[1].parse().unwrap_or(0);
        Some((ahead, behind))
    } else {
        Some((0, 0))
    }
}

/// Get repository status
pub fn get_repo_status(repo_info: &RepoInfo) -> RepoStatus {
    if !path_exists(&repo_info.absolute_path) {
        return RepoStatus {
            name: repo_info.name.clone(),
            branch: String::new(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: false,
            state: RepositoryState::Clean,
        };
    }

    let state = RepositoryState::detect(&repo_info.absolute_path);

    match get_cached_status(&repo_info.absolute_path) {
        Ok(status) => {
            // Get ahead/behind counts vs target branch
            let (ahead_main, behind_main) =
                get_ahead_behind_branch(&repo_info.absolute_path, &repo_info.revision)
                    .unwrap_or((0, 0));

            RepoStatus {
                name: repo_info.name.clone(),
                branch: status.current_branch,
                clean: status.is_clean,
                staged: status.staged.len(),
                modified: status.modified.len(),
                untracked: status.untracked.len(),
                ahead: status.ahead,
                behind: status.behind,
                ahead_main,
                behind_main,
                exists: true,
                state,
            }
        }
        Err(_) => RepoStatus {
            name: repo_info.name.clone(),
            branch: "error".to_string(),
            clean: true,
            staged: 0,
            modified: 0,
            untracked: 0,
            ahead: 0,
            behind: 0,
            ahead_main: 0,
            behind_main: 0,
            exists: true,
            state,
        },
    }
}

/// Get status for all repositories
pub fn get_all_repo_status(repos: &[RepoInfo]) -> Vec<RepoStatus> {
    repos.iter().map(get_repo_status).collect()
}

/// Get list of changed files (staged, modified, and untracked)
pub fn get_changed_files(repo: &Repository) -> Result<Vec<String>, GitError> {
    let status = get_status_info(repo)?;
    let mut files = status.staged;
    files.extend(status.modified);
    files.extend(status.untracked);
    Ok(files)
}

/// Check if there are uncommitted changes
pub fn has_uncommitted_changes(repo: &Repository) -> Result<bool, GitError> {
    let status = get_status_info(repo)?;
    Ok(!status.is_clean)
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

        let repo = open_repo(temp.path()).unwrap();
        (temp, repo)
    }

    #[test]
    fn test_clean_repo() {
        let (temp, repo) = setup_test_repo();

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

        let status = get_status_info(&repo).unwrap();
        assert!(status.is_clean);
        assert!(status.staged.is_empty());
        assert!(status.modified.is_empty());
        assert!(status.untracked.is_empty());
    }

    #[test]
    fn test_untracked_file() {
        let (temp, repo) = setup_test_repo();

        // Create initial commit first
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

        // Create an untracked file
        fs::write(temp.path().join("new_file.txt"), "content").unwrap();

        let status = get_status_info(&repo).unwrap();
        assert!(!status.is_clean);
        assert!(status.staged.is_empty());
        assert!(status.modified.is_empty());
        assert_eq!(status.untracked.len(), 1);
        assert!(status.untracked.contains(&"new_file.txt".to_string()));
    }

    #[test]
    fn test_staged_file() {
        let (temp, repo) = setup_test_repo();

        // Create initial commit first
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

        // Create and stage a file
        fs::write(temp.path().join("staged.txt"), "content").unwrap();
        Command::new("git")
            .args(["add", "staged.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let status = get_status_info(&repo).unwrap();
        assert!(!status.is_clean);
        assert_eq!(status.staged.len(), 1);
        assert!(status.staged.contains(&"staged.txt".to_string()));
    }

    #[test]
    fn test_parse_ahead_behind_valid() {
        let output = b"3\t5";
        let result = parse_ahead_behind(output);
        assert_eq!(result, Some((5, 3)));
    }

    #[test]
    fn test_parse_ahead_behind_zeros() {
        let output = b"0\t0";
        let result = parse_ahead_behind(output);
        assert_eq!(result, Some((0, 0)));
    }

    #[test]
    fn test_parse_ahead_behind_empty() {
        let output = b"";
        let result = parse_ahead_behind(output);
        assert_eq!(result, Some((0, 0)));
    }

    #[test]
    fn test_parse_ahead_behind_invalid() {
        let output = b"notanumber\talsonotanumber";
        let result = parse_ahead_behind(output);
        assert_eq!(result, Some((0, 0)));
    }

    #[test]
    fn test_parse_ahead_behind_with_newline() {
        let output = b"2\t7\n";
        let result = parse_ahead_behind(output);
        assert_eq!(result, Some((7, 2)));
    }

    #[test]
    fn test_modified_file() {
        let (temp, repo) = setup_test_repo();

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

        // Modify the committed file
        fs::write(temp.path().join("README.md"), "# Modified").unwrap();

        let status = get_status_info(&repo).unwrap();
        assert!(!status.is_clean);
        assert_eq!(status.modified.len(), 1);
        assert!(status.modified.contains(&"README.md".to_string()));
    }

    #[test]
    fn test_has_uncommitted_changes() {
        let (temp, repo) = setup_test_repo();

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

        assert!(!has_uncommitted_changes(&repo).unwrap());

        // Create untracked file
        fs::write(temp.path().join("new.txt"), "content").unwrap();
        assert!(has_uncommitted_changes(&repo).unwrap());
    }

    #[test]
    fn test_repository_state_clean() {
        let (temp, _repo) = setup_test_repo();
        assert_eq!(RepositoryState::detect(temp.path()), RepositoryState::Clean);
    }

    #[test]
    fn test_repository_state_rebasing() {
        let (temp, _repo) = setup_test_repo();
        fs::create_dir_all(temp.path().join(".git/rebase-merge")).unwrap();
        assert_eq!(
            RepositoryState::detect(temp.path()),
            RepositoryState::Rebasing
        );
    }

    #[test]
    fn test_repository_state_rebase_apply() {
        let (temp, _repo) = setup_test_repo();
        fs::create_dir_all(temp.path().join(".git/rebase-apply")).unwrap();
        assert_eq!(
            RepositoryState::detect(temp.path()),
            RepositoryState::Rebasing
        );
    }

    #[test]
    fn test_repository_state_merging() {
        let (temp, _repo) = setup_test_repo();
        fs::write(temp.path().join(".git/MERGE_HEAD"), "abc123").unwrap();
        assert_eq!(
            RepositoryState::detect(temp.path()),
            RepositoryState::Merging
        );
    }

    #[test]
    fn test_repository_state_cherry_picking() {
        let (temp, _repo) = setup_test_repo();
        fs::write(temp.path().join(".git/CHERRY_PICK_HEAD"), "abc123").unwrap();
        assert_eq!(
            RepositoryState::detect(temp.path()),
            RepositoryState::CherryPicking
        );
    }

    #[test]
    fn test_repository_state_labels() {
        assert_eq!(RepositoryState::Clean.label(), None);
        assert_eq!(RepositoryState::Rebasing.label(), Some("REBASING"));
        assert_eq!(RepositoryState::Merging.label(), Some("MERGING"));
        assert_eq!(
            RepositoryState::CherryPicking.label(),
            Some("CHERRY-PICKING")
        );
    }

    #[test]
    fn test_get_changed_files() {
        let (temp, repo) = setup_test_repo();

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

        // Stage a new file
        fs::write(temp.path().join("staged.txt"), "staged").unwrap();
        Command::new("git")
            .args(["add", "staged.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        // Create an untracked file
        fs::write(temp.path().join("untracked.txt"), "untracked").unwrap();

        let changed = get_changed_files(&repo).unwrap();
        assert!(changed.contains(&"staged.txt".to_string()));
        assert!(changed.contains(&"untracked.txt".to_string()));
    }
}
