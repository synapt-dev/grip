//! Forall command implementation
//!
//! Runs a command in each repository.
//!
//! Includes optimization: common git commands are intercepted and run using
//! the git2/gix library instead of spawning git CLI processes, providing
//! up to 100x speedup.
//!
//! Supports:
//! - Direct git commands (git status, git branch, etc.)
//! - Piped commands (git status | grep modified)
//! - Redirected commands (git log > file.txt)

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::path_exists;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// Diff output format
#[derive(Debug, Clone)]
enum DiffFormat {
    Stat,
    NameOnly,
    NameStatus,
    Patch,
}

/// Interceptable git commands for optimization
#[derive(Debug, Clone)]
enum GitCommand {
    /// git status [--porcelain|-s]
    Status { porcelain: bool },
    /// git branch [-a|-r]
    ListBranches { all: bool, remotes: bool },
    /// git rev-parse HEAD
    GetHead,
    /// git rev-parse --abbrev-ref HEAD
    GetBranch,
    /// git rev-parse --short HEAD
    GetHeadShort,
    /// git log --oneline [-n N]
    LogOneline { count: usize },
    /// git diff [--staged] [format]
    Diff { staged: bool, format: DiffFormat },
    /// git ls-files [-m]
    LsFiles { modified: bool },
    /// git tag [-l]
    TagList,
    /// git remote [-v]
    Remote { verbose: bool },
    /// git stash list
    StashList,
    /// git blame FILE
    Blame { file: String },
    /// git config --get KEY
    ConfigGet { key: String },
}

/// Parsed command with optional pipe or redirect
#[derive(Debug)]
enum ParsedCommand {
    /// Simple git command
    Git(GitCommand),
    /// Git command piped to another command
    Piped {
        git_cmd: GitCommand,
        pipe_to: String,
    },
    /// Git command redirected to file
    Redirect {
        git_cmd: GitCommand,
        file: String,
        append: bool,
    },
    /// Not interceptable, run as shell command
    Shell(String),
}

/// Parse a command string, handling pipes and redirects
fn parse_command(command: &str) -> ParsedCommand {
    let trimmed = command.trim();

    // Check for pipe
    if let Some(pipe_pos) = trimmed.find('|') {
        let git_part = trimmed[..pipe_pos].trim();
        let pipe_part = trimmed[pipe_pos + 1..].trim();

        if let Some(git_cmd) = try_parse_git_command(git_part) {
            return ParsedCommand::Piped {
                git_cmd,
                pipe_to: pipe_part.to_string(),
            };
        }
    }

    // Check for redirect (>> before >)
    if let Some(pos) = trimmed.find(">>") {
        let git_part = trimmed[..pos].trim();
        let file = trimmed[pos + 2..].trim();

        if let Some(git_cmd) = try_parse_git_command(git_part) {
            return ParsedCommand::Redirect {
                git_cmd,
                file: file.to_string(),
                append: true,
            };
        }
    } else if let Some(pos) = trimmed.find('>') {
        let git_part = trimmed[..pos].trim();
        let file = trimmed[pos + 1..].trim();

        if let Some(git_cmd) = try_parse_git_command(git_part) {
            return ParsedCommand::Redirect {
                git_cmd,
                file: file.to_string(),
                append: false,
            };
        }
    }

    // Try simple git command
    if let Some(git_cmd) = try_parse_git_command(trimmed) {
        return ParsedCommand::Git(git_cmd);
    }

    // Fall back to shell
    ParsedCommand::Shell(command.to_string())
}

/// Try to parse a command string into an interceptable GitCommand
fn try_parse_git_command(command: &str) -> Option<GitCommand> {
    let trimmed = command.trim();
    let parts: Vec<&str> = trimmed.split_whitespace().collect();

    match parts.as_slice() {
        // === STATUS ===
        ["git", "status"] => Some(GitCommand::Status { porcelain: false }),
        ["git", "status", "--porcelain"] => Some(GitCommand::Status { porcelain: true }),
        ["git", "status", "-s"] => Some(GitCommand::Status { porcelain: true }),
        ["git", "status", "--short"] => Some(GitCommand::Status { porcelain: true }),

        // === BRANCH ===
        ["git", "branch"] => Some(GitCommand::ListBranches {
            all: false,
            remotes: false,
        }),
        ["git", "branch", "-a"] => Some(GitCommand::ListBranches {
            all: true,
            remotes: false,
        }),
        ["git", "branch", "--all"] => Some(GitCommand::ListBranches {
            all: true,
            remotes: false,
        }),
        ["git", "branch", "-r"] => Some(GitCommand::ListBranches {
            all: false,
            remotes: true,
        }),
        ["git", "branch", "--remotes"] => Some(GitCommand::ListBranches {
            all: false,
            remotes: true,
        }),

        // === REV-PARSE ===
        ["git", "rev-parse", "HEAD"] => Some(GitCommand::GetHead),
        ["git", "rev-parse", "--abbrev-ref", "HEAD"] => Some(GitCommand::GetBranch),
        ["git", "rev-parse", "--short", "HEAD"] => Some(GitCommand::GetHeadShort),

        // === LOG ===
        ["git", "log", "--oneline"] => Some(GitCommand::LogOneline { count: 10 }),
        ["git", "log", "--oneline", "-n", n] => {
            n.parse().ok().map(|count| GitCommand::LogOneline { count })
        }
        ["git", "log", "--oneline", n] if n.starts_with('-') => n[1..]
            .parse()
            .ok()
            .map(|count| GitCommand::LogOneline { count }),
        ["git", "log", "-1", "--oneline"] => Some(GitCommand::LogOneline { count: 1 }),
        ["git", "log", n, "--oneline"] if n.starts_with('-') => n[1..]
            .parse()
            .ok()
            .map(|count| GitCommand::LogOneline { count }),

        // === DIFF ===
        ["git", "diff"] => Some(GitCommand::Diff {
            staged: false,
            format: DiffFormat::Patch,
        }),
        ["git", "diff", "--stat"] => Some(GitCommand::Diff {
            staged: false,
            format: DiffFormat::Stat,
        }),
        ["git", "diff", "--name-only"] => Some(GitCommand::Diff {
            staged: false,
            format: DiffFormat::NameOnly,
        }),
        ["git", "diff", "--name-status"] => Some(GitCommand::Diff {
            staged: false,
            format: DiffFormat::NameStatus,
        }),
        ["git", "diff", "--staged"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::Patch,
        }),
        ["git", "diff", "--cached"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::Patch,
        }),
        ["git", "diff", "--staged", "--stat"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::Stat,
        }),
        ["git", "diff", "--cached", "--stat"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::Stat,
        }),
        ["git", "diff", "--staged", "--name-only"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::NameOnly,
        }),
        ["git", "diff", "--cached", "--name-only"] => Some(GitCommand::Diff {
            staged: true,
            format: DiffFormat::NameOnly,
        }),

        // === LS-FILES ===
        ["git", "ls-files"] => Some(GitCommand::LsFiles { modified: false }),
        ["git", "ls-files", "-m"] => Some(GitCommand::LsFiles { modified: true }),
        ["git", "ls-files", "--modified"] => Some(GitCommand::LsFiles { modified: true }),

        // === TAG ===
        ["git", "tag"] => Some(GitCommand::TagList),
        ["git", "tag", "-l"] => Some(GitCommand::TagList),
        ["git", "tag", "--list"] => Some(GitCommand::TagList),

        // === REMOTE ===
        ["git", "remote"] => Some(GitCommand::Remote { verbose: false }),
        ["git", "remote", "-v"] => Some(GitCommand::Remote { verbose: true }),
        ["git", "remote", "--verbose"] => Some(GitCommand::Remote { verbose: true }),

        // === STASH ===
        ["git", "stash", "list"] => Some(GitCommand::StashList),

        // === BLAME ===
        ["git", "blame", file] => Some(GitCommand::Blame {
            file: file.to_string(),
        }),

        // === CONFIG ===
        ["git", "config", "--get", key] => Some(GitCommand::ConfigGet {
            key: key.to_string(),
        }),

        _ => None,
    }
}

/// Execute an intercepted git command using git2 (fast path)
fn execute_git_command(repo_path: &PathBuf, cmd: &GitCommand) -> Result<String, String> {
    let repo =
        crate::git::open_repo(repo_path).map_err(|e| format!("Failed to open repo: {}", e))?;

    match cmd {
        GitCommand::Status { porcelain } => execute_status(&repo, *porcelain),
        GitCommand::ListBranches { all, remotes } => execute_branches(&repo, *all, *remotes),
        GitCommand::GetHead => execute_get_head(&repo),
        GitCommand::GetBranch => execute_get_branch(&repo),
        GitCommand::GetHeadShort => execute_get_head_short(&repo),
        GitCommand::LogOneline { count } => execute_log_oneline(&repo, *count),
        GitCommand::Diff { staged, format } => execute_diff(&repo, *staged, format),
        GitCommand::LsFiles { modified } => execute_ls_files(&repo, *modified),
        GitCommand::TagList => execute_tag_list(&repo),
        GitCommand::Remote { verbose } => execute_remote(&repo, *verbose),
        GitCommand::StashList => execute_stash_list(&repo),
        GitCommand::Blame { file } => execute_blame(&repo, repo_path, file),
        GitCommand::ConfigGet { key } => execute_config_get(&repo, key),
    }
}

fn execute_status(repo: &git2::Repository, porcelain: bool) -> Result<String, String> {
    let statuses = repo
        .statuses(None)
        .map_err(|e| format!("Failed to get status: {}", e))?;

    if porcelain {
        let mut output = String::new();
        for entry in statuses.iter() {
            let status = entry.status();
            let path = entry.path().unwrap_or("?");

            let index_status = if status.is_index_new() {
                'A'
            } else if status.is_index_modified() {
                'M'
            } else if status.is_index_deleted() {
                'D'
            } else if status.is_index_renamed() {
                'R'
            } else if status.is_index_typechange() {
                'T'
            } else {
                ' '
            };

            let wt_status = if status.is_wt_new() {
                '?'
            } else if status.is_wt_modified() {
                'M'
            } else if status.is_wt_deleted() {
                'D'
            } else if status.is_wt_renamed() {
                'R'
            } else if status.is_wt_typechange() {
                'T'
            } else {
                ' '
            };

            output.push_str(&format!("{}{} {}\n", index_status, wt_status, path));
        }
        Ok(output)
    } else {
        if statuses.is_empty() {
            return Ok("nothing to commit, working tree clean\n".to_string());
        }

        let mut output = String::new();
        let mut staged = Vec::new();
        let mut unstaged = Vec::new();
        let mut untracked = Vec::new();

        for entry in statuses.iter() {
            let path = entry.path().unwrap_or("?").to_string();
            let status = entry.status();

            if status.is_index_new() || status.is_index_modified() || status.is_index_deleted() {
                staged.push(path.clone());
            }
            if status.is_wt_modified() || status.is_wt_deleted() {
                unstaged.push(path.clone());
            }
            if status.is_wt_new() {
                untracked.push(path);
            }
        }

        if !staged.is_empty() {
            output.push_str("Changes to be committed:\n");
            for f in &staged {
                output.push_str(&format!("  {}\n", f));
            }
        }
        if !unstaged.is_empty() {
            output.push_str("Changes not staged for commit:\n");
            for f in &unstaged {
                output.push_str(&format!("  {}\n", f));
            }
        }
        if !untracked.is_empty() {
            output.push_str("Untracked files:\n");
            for f in &untracked {
                output.push_str(&format!("  {}\n", f));
            }
        }
        Ok(output)
    }
}

fn execute_branches(repo: &git2::Repository, all: bool, remotes: bool) -> Result<String, String> {
    let mut output = String::new();
    let head = repo.head().ok();
    let current_branch = head.as_ref().and_then(|h| h.shorthand()).unwrap_or("");

    // Local branches (unless remotes-only)
    if !remotes {
        let branches = repo
            .branches(Some(git2::BranchType::Local))
            .map_err(|e| format!("Failed to list branches: {}", e))?;

        for branch in branches {
            let (branch, _) = branch.map_err(|e| format!("Failed to read branch: {}", e))?;
            let name = branch
                .name()
                .map_err(|e| format!("Failed to get branch name: {}", e))?
                .unwrap_or("?");

            if name == current_branch {
                output.push_str(&format!("* {}\n", name));
            } else {
                output.push_str(&format!("  {}\n", name));
            }
        }
    }

    // Remote branches if -a or -r flag
    if all || remotes {
        let remote_branches = repo
            .branches(Some(git2::BranchType::Remote))
            .map_err(|e| format!("Failed to list remote branches: {}", e))?;

        for branch in remote_branches {
            let (branch, _) = branch.map_err(|e| format!("Failed to read branch: {}", e))?;
            let name = branch
                .name()
                .map_err(|e| format!("Failed to get branch name: {}", e))?
                .unwrap_or("?");
            output.push_str(&format!("  remotes/{}\n", name));
        }
    }

    Ok(output)
}

fn execute_get_head(repo: &git2::Repository) -> Result<String, String> {
    let head = repo
        .head()
        .map_err(|e| format!("Failed to get HEAD: {}", e))?;
    let oid = head
        .target()
        .ok_or_else(|| "HEAD has no target".to_string())?;
    Ok(format!("{}\n", oid))
}

fn execute_get_branch(repo: &git2::Repository) -> Result<String, String> {
    let head = repo
        .head()
        .map_err(|e| format!("Failed to get HEAD: {}", e))?;
    let name = head.shorthand().unwrap_or("HEAD");
    Ok(format!("{}\n", name))
}

fn execute_get_head_short(repo: &git2::Repository) -> Result<String, String> {
    let head = repo
        .head()
        .map_err(|e| format!("Failed to get HEAD: {}", e))?;
    let oid = head
        .target()
        .ok_or_else(|| "HEAD has no target".to_string())?;
    Ok(format!("{}\n", &oid.to_string()[..7]))
}

fn execute_log_oneline(repo: &git2::Repository, count: usize) -> Result<String, String> {
    let mut revwalk = repo
        .revwalk()
        .map_err(|e| format!("Failed to create revwalk: {}", e))?;
    revwalk
        .push_head()
        .map_err(|e| format!("Failed to push HEAD: {}", e))?;

    let mut output = String::new();
    for oid in revwalk.take(count) {
        let oid = oid.map_err(|e| format!("Failed to get oid: {}", e))?;
        let commit = repo
            .find_commit(oid)
            .map_err(|e| format!("Failed to find commit: {}", e))?;
        let short = &oid.to_string()[..7];
        let msg = commit.summary().unwrap_or("");
        output.push_str(&format!("{} {}\n", short, msg));
    }
    Ok(output)
}

fn execute_diff(
    repo: &git2::Repository,
    staged: bool,
    format: &DiffFormat,
) -> Result<String, String> {
    let diff = if staged {
        let head = repo
            .head()
            .map_err(|e| format!("Failed to get HEAD: {}", e))?;
        let tree = head
            .peel_to_tree()
            .map_err(|e| format!("Failed to get tree: {}", e))?;
        repo.diff_tree_to_index(Some(&tree), None, None)
    } else {
        repo.diff_index_to_workdir(None, None)
    }
    .map_err(|e| format!("Failed to get diff: {}", e))?;

    match format {
        DiffFormat::Stat => {
            let stats = diff
                .stats()
                .map_err(|e| format!("Failed to get stats: {}", e))?;
            let mut output = String::new();

            for delta in diff.deltas() {
                let path = delta
                    .new_file()
                    .path()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "?".to_string());
                output.push_str(&format!(" {} |\n", path));
            }

            output.push_str(&format!(
                " {} files changed, {} insertions(+), {} deletions(-)\n",
                stats.files_changed(),
                stats.insertions(),
                stats.deletions()
            ));
            Ok(output)
        }
        DiffFormat::NameOnly => {
            let output: Vec<String> = diff
                .deltas()
                .filter_map(|d| d.new_file().path().map(|p| p.display().to_string()))
                .collect();
            Ok(output.join("\n") + if output.is_empty() { "" } else { "\n" })
        }
        DiffFormat::NameStatus => {
            let mut output = String::new();
            for delta in diff.deltas() {
                let status = match delta.status() {
                    git2::Delta::Added => 'A',
                    git2::Delta::Deleted => 'D',
                    git2::Delta::Modified => 'M',
                    git2::Delta::Renamed => 'R',
                    git2::Delta::Copied => 'C',
                    _ => '?',
                };
                let path = delta
                    .new_file()
                    .path()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "?".to_string());
                output.push_str(&format!("{}\t{}\n", status, path));
            }
            Ok(output)
        }
        DiffFormat::Patch => {
            let mut output = String::new();
            diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
                let prefix = match line.origin() {
                    '+' => "+",
                    '-' => "-",
                    ' ' => " ",
                    'H' => "", // hunk header
                    'F' => "", // file header
                    _ => "",
                };
                if let Ok(content) = std::str::from_utf8(line.content()) {
                    if !prefix.is_empty() || line.origin() == 'H' || line.origin() == 'F' {
                        output.push_str(prefix);
                        output.push_str(content);
                    }
                }
                true
            })
            .map_err(|e| format!("Failed to print diff: {}", e))?;
            Ok(output)
        }
    }
}

fn execute_ls_files(repo: &git2::Repository, modified: bool) -> Result<String, String> {
    if modified {
        let statuses = repo
            .statuses(None)
            .map_err(|e| format!("Failed to get status: {}", e))?;
        let files: Vec<String> = statuses
            .iter()
            .filter(|e| e.status().is_wt_modified() || e.status().is_index_modified())
            .filter_map(|e| e.path().map(String::from))
            .collect();
        Ok(files.join("\n") + if files.is_empty() { "" } else { "\n" })
    } else {
        let index = repo
            .index()
            .map_err(|e| format!("Failed to get index: {}", e))?;
        let files: Vec<String> = index
            .iter()
            .filter_map(|e| String::from_utf8(e.path.clone()).ok())
            .collect();
        Ok(files.join("\n") + if files.is_empty() { "" } else { "\n" })
    }
}

fn execute_tag_list(repo: &git2::Repository) -> Result<String, String> {
    let mut tags = Vec::new();
    repo.tag_foreach(|_, name| {
        if let Ok(name) = std::str::from_utf8(name) {
            let name = name.strip_prefix("refs/tags/").unwrap_or(name);
            tags.push(name.to_string());
        }
        true
    })
    .map_err(|e| format!("Failed to list tags: {}", e))?;
    tags.sort();
    Ok(tags.join("\n") + if tags.is_empty() { "" } else { "\n" })
}

fn execute_remote(repo: &git2::Repository, verbose: bool) -> Result<String, String> {
    let remotes = repo
        .remotes()
        .map_err(|e| format!("Failed to get remotes: {}", e))?;
    let mut output = String::new();

    for name in remotes.iter().flatten() {
        if verbose {
            if let Ok(remote) = repo.find_remote(name) {
                let url = remote.url().unwrap_or("");
                output.push_str(&format!("{}\t{} (fetch)\n", name, url));
                output.push_str(&format!(
                    "{}\t{} (push)\n",
                    name,
                    remote.pushurl().unwrap_or(url)
                ));
            }
        } else {
            output.push_str(&format!("{}\n", name));
        }
    }
    Ok(output)
}

fn execute_stash_list(repo: &git2::Repository) -> Result<String, String> {
    // git2's stash_foreach requires &mut self, so we iterate refs instead
    let mut stashes = Vec::new();

    // Stashes are stored as refs/stash with reflog entries
    if let Ok(reference) = repo.find_reference("refs/stash") {
        if let Ok(reflog) = repo.reflog("refs/stash") {
            for (idx, entry) in reflog.iter().enumerate() {
                let msg = entry.message().unwrap_or("");
                stashes.push(format!("stash@{{{}}}: {}", idx, msg));
            }
        } else if let Some(oid) = reference.target() {
            // If no reflog, at least show the current stash
            if let Ok(commit) = repo.find_commit(oid) {
                let msg = commit.summary().unwrap_or("WIP");
                stashes.push(format!(
                    "stash@{{0}}: On {}: {}",
                    commit
                        .parent(0)
                        .ok()
                        .and_then(|_| repo.head().ok())
                        .and_then(|h| h.shorthand().map(String::from))
                        .unwrap_or_else(|| "branch".to_string()),
                    msg
                ));
            }
        }
    }

    Ok(stashes.join("\n") + if stashes.is_empty() { "" } else { "\n" })
}

fn execute_blame(repo: &git2::Repository, repo_path: &Path, file: &str) -> Result<String, String> {
    let blame = repo
        .blame_file(Path::new(file), None)
        .map_err(|e| format!("Failed to blame file: {}", e))?;

    let workdir = repo.workdir().unwrap_or(repo_path);
    let file_path = workdir.join(file);
    let content =
        std::fs::read_to_string(&file_path).map_err(|e| format!("Failed to read file: {}", e))?;
    let lines: Vec<&str> = content.lines().collect();

    let mut output = String::new();
    let mut line_num = 1;

    for hunk in blame.iter() {
        let oid = hunk.final_commit_id();
        let sig = hunk.final_signature();
        let short = &oid.to_string()[..8];
        let author = sig.name().unwrap_or("?");

        for _ in 0..hunk.lines_in_hunk() {
            let line_content = lines.get(line_num - 1).unwrap_or(&"");
            output.push_str(&format!(
                "{} ({:>12} {:>4}) {}\n",
                short, author, line_num, line_content
            ));
            line_num += 1;
        }
    }
    Ok(output)
}

fn execute_config_get(repo: &git2::Repository, key: &str) -> Result<String, String> {
    let config = repo
        .config()
        .map_err(|e| format!("Failed to get config: {}", e))?;
    let value = config.get_string(key).unwrap_or_default();
    Ok(format!("{}\n", value))
}

/// Execute a piped command: run git fast, pipe to shell command
fn execute_piped_command(
    repo_path: &PathBuf,
    git_cmd: &GitCommand,
    pipe_to: &str,
) -> Result<String, String> {
    let git_output = execute_git_command(repo_path, git_cmd)?;

    let mut child = Command::new("sh")
        .arg("-c")
        .arg(pipe_to)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn pipe command: {}", e))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(git_output.as_bytes())
            .map_err(|e| format!("Failed to write to pipe: {}", e))?;
    }

    let output = child
        .wait_with_output()
        .map_err(|e| format!("Failed to wait for pipe command: {}", e))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        Err(format!(
            "Pipe command failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ))
    }
}

/// Execute a redirected command: run git fast, write to file
fn execute_redirected_command(
    repo_path: &PathBuf,
    git_cmd: &GitCommand,
    file: &str,
    append: bool,
) -> Result<String, String> {
    let git_output = execute_git_command(repo_path, git_cmd)?;

    let mut file_handle = if append {
        std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(file)
    } else {
        std::fs::File::create(file)
    }
    .map_err(|e| format!("Failed to open file '{}': {}", file, e))?;

    file_handle
        .write_all(git_output.as_bytes())
        .map_err(|e| format!("Failed to write to file: {}", e))?;

    Ok(format!("Output written to {}\n", file))
}

/// Run the forall command
pub fn run_forall(
    workspace_root: &Path,
    manifest: &Manifest,
    command: &str,
    parallel: bool,
    changed_only: bool,
    no_intercept: bool,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let repos: Vec<RepoInfo> = filter_repos(manifest, workspace_root, None, group_filter, true);

    // Parse the command (handles pipes, redirects, git interception)
    let parsed = if no_intercept {
        ParsedCommand::Shell(command.to_string())
    } else {
        parse_command(command)
    };

    if parallel {
        run_parallel(&repos, command, changed_only, &parsed)?;
    } else {
        run_sequential(&repos, command, changed_only, &parsed)?;
    }

    Ok(())
}

/// Execute a parsed command for a single repo
fn execute_parsed_command(
    repo_path: &PathBuf,
    command: &str,
    parsed: &ParsedCommand,
    repo: &RepoInfo,
) -> Result<String, String> {
    match parsed {
        ParsedCommand::Git(git_cmd) => execute_git_command(repo_path, git_cmd),
        ParsedCommand::Piped { git_cmd, pipe_to } => {
            execute_piped_command(repo_path, git_cmd, pipe_to)
        }
        ParsedCommand::Redirect {
            git_cmd,
            file,
            append,
        } => execute_redirected_command(repo_path, git_cmd, file, *append),
        ParsedCommand::Shell(_) => {
            // Run as shell command
            let output = Command::new("sh")
                .arg("-c")
                .arg(command)
                .current_dir(repo_path)
                .env("REPO_NAME", &repo.name)
                .env("REPO_PATH", repo_path)
                .env("REPO_URL", &repo.url)
                .env("REPO_BRANCH", &repo.revision)
                .output()
                .map_err(|e| e.to_string())?;

            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout).to_string();
                let stderr = String::from_utf8_lossy(&output.stderr).to_string();
                Ok(format!("{}{}", stdout, stderr))
            } else {
                Err(format!(
                    "Exit code: {:?}\n{}{}",
                    output.status.code(),
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                ))
            }
        }
    }
}

fn run_sequential(
    repos: &[RepoInfo],
    command: &str,
    changed_only: bool,
    parsed: &ParsedCommand,
) -> anyhow::Result<()> {
    let mut success_count = 0;
    let mut error_count = 0;
    let mut skip_count = 0;

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            Output::warning(&format!("{}: not cloned, skipping", repo.name));
            skip_count += 1;
            continue;
        }

        if changed_only && !has_changes(&repo.absolute_path)? {
            skip_count += 1;
            continue;
        }

        Output::header(&format!("{}:", repo.name));

        match execute_parsed_command(&repo.absolute_path, command, parsed, repo) {
            Ok(output) => {
                print!("{}", output);
                success_count += 1;
            }
            Err(e) => {
                Output::error(&e);
                error_count += 1;
            }
        }
        println!();
    }

    // Summary
    if error_count == 0 {
        Output::success(&format!(
            "Command completed in {} repo(s){}",
            success_count,
            if skip_count > 0 {
                format!(", {} skipped", skip_count)
            } else {
                String::new()
            }
        ));
    } else {
        Output::warning(&format!(
            "{} succeeded, {} failed, {} skipped",
            success_count, error_count, skip_count
        ));
    }

    Ok(())
}

/// Cloneable version of ParsedCommand for use in threads
#[derive(Debug, Clone)]
#[allow(dead_code)]
enum CloneableParsedCommand {
    Git(GitCommand),
    Piped {
        git_cmd: GitCommand,
        pipe_to: String,
    },
    Redirect {
        git_cmd: GitCommand,
        file: String,
        append: bool,
    },
    Shell(String), // String kept for structural consistency with ParsedCommand
}

impl From<&ParsedCommand> for CloneableParsedCommand {
    fn from(cmd: &ParsedCommand) -> Self {
        match cmd {
            ParsedCommand::Git(git_cmd) => CloneableParsedCommand::Git(git_cmd.clone()),
            ParsedCommand::Piped { git_cmd, pipe_to } => CloneableParsedCommand::Piped {
                git_cmd: git_cmd.clone(),
                pipe_to: pipe_to.clone(),
            },
            ParsedCommand::Redirect {
                git_cmd,
                file,
                append,
            } => CloneableParsedCommand::Redirect {
                git_cmd: git_cmd.clone(),
                file: file.clone(),
                append: *append,
            },
            ParsedCommand::Shell(s) => CloneableParsedCommand::Shell(s.clone()),
        }
    }
}

fn run_parallel(
    repos: &[RepoInfo],
    command: &str,
    changed_only: bool,
    parsed: &ParsedCommand,
) -> anyhow::Result<()> {
    use std::sync::{Arc, Mutex};
    use std::thread;

    let results = Arc::new(Mutex::new(Vec::new()));
    let mut handles = vec![];

    // Clone the parsed command for threads
    let cloneable_cmd = CloneableParsedCommand::from(parsed);

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        if changed_only && !has_changes(&repo.absolute_path).unwrap_or(false) {
            continue;
        }

        let repo_name = repo.name.clone();
        let repo_path = repo.absolute_path.clone();
        let repo_url = repo.url.clone();
        let repo_branch = repo.revision.clone();
        let cmd = command.to_string();
        let results = Arc::clone(&results);
        let parsed_cmd = cloneable_cmd.clone();

        let handle = thread::spawn(move || {
            let result = match &parsed_cmd {
                CloneableParsedCommand::Git(git_cmd) => execute_git_command(&repo_path, git_cmd),
                CloneableParsedCommand::Piped { git_cmd, pipe_to } => {
                    execute_piped_command(&repo_path, git_cmd, pipe_to)
                }
                CloneableParsedCommand::Redirect {
                    git_cmd,
                    file,
                    append,
                } => execute_redirected_command(&repo_path, git_cmd, file, *append),
                CloneableParsedCommand::Shell(_) => {
                    // Run as shell command
                    let output = Command::new("sh")
                        .arg("-c")
                        .arg(&cmd)
                        .current_dir(&repo_path)
                        .env("REPO_NAME", &repo_name)
                        .env("REPO_PATH", &repo_path)
                        .env("REPO_URL", &repo_url)
                        .env("REPO_BRANCH", &repo_branch)
                        .output();

                    match output {
                        Ok(out) => {
                            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
                            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
                            if out.status.success() {
                                Ok(format!("{}{}", stdout, stderr))
                            } else {
                                Err(format!(
                                    "Exit code: {:?}\n{}{}",
                                    out.status.code(),
                                    stdout,
                                    stderr
                                ))
                            }
                        }
                        Err(e) => Err(e.to_string()),
                    }
                }
            };

            let mut results = results.lock().expect("mutex poisoned");
            results.push((repo_name, result));
        });

        handles.push(handle);
    }

    // Wait for all threads
    for handle in handles {
        handle
            .join()
            .map_err(|_| anyhow::anyhow!("Worker thread panicked"))?;
    }

    // Print results
    let results = results.lock().expect("mutex poisoned");
    let mut success_count = 0;
    let mut error_count = 0;

    for (repo_name, output) in results.iter() {
        Output::header(&format!("{}:", repo_name));
        match output {
            Ok(output) => {
                print!("{}", output);
                success_count += 1;
            }
            Err(e) => {
                Output::error(&e.to_string());
                error_count += 1;
            }
        }
        println!();
    }

    if error_count == 0 {
        Output::success(&format!("Command completed in {} repo(s)", success_count));
    } else {
        Output::warning(&format!(
            "{} succeeded, {} failed",
            success_count, error_count
        ));
    }

    Ok(())
}

/// Check if a repository has uncommitted changes
fn has_changes(repo_path: &PathBuf) -> anyhow::Result<bool> {
    match crate::git::open_repo(repo_path) {
        Ok(repo) => {
            let statuses = repo.statuses(None)?;
            Ok(!statuses.is_empty())
        }
        Err(_) => Ok(false),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use git2::Repository;
    use tempfile::TempDir;

    fn setup_test_repo(temp: &TempDir) -> PathBuf {
        let repo_path = temp.path().join("repo");
        std::fs::create_dir_all(&repo_path).unwrap();
        let repo = Repository::init(&repo_path).unwrap();

        // Configure git
        {
            let mut config = repo.config().unwrap();
            config.set_str("user.name", "Test User").unwrap();
            config.set_str("user.email", "test@example.com").unwrap();
        }

        // Create initial commit
        {
            std::fs::write(repo_path.join("README.md"), "# Test").unwrap();
            let mut index = repo.index().unwrap();
            index.add_path(std::path::Path::new("README.md")).unwrap();
            index.write().unwrap();
            let sig = repo.signature().unwrap();
            let tree_id = index.write_tree().unwrap();
            let tree = repo.find_tree(tree_id).unwrap();
            repo.commit(Some("HEAD"), &sig, &sig, "Initial commit", &tree, &[])
                .unwrap();
        }

        repo_path
    }

    #[test]
    fn test_has_changes_clean_repo() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        let result = has_changes(&repo_path);
        assert!(result.is_ok());
        assert!(!result.unwrap()); // Clean repo has no changes
    }

    #[test]
    fn test_has_changes_with_modifications() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        // Modify a tracked file
        std::fs::write(repo_path.join("README.md"), "# Modified").unwrap();

        let result = has_changes(&repo_path);
        assert!(result.is_ok());
        assert!(result.unwrap()); // Has modifications
    }

    #[test]
    fn test_has_changes_with_untracked_file() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        // Add untracked file
        std::fs::write(repo_path.join("new-file.txt"), "content").unwrap();

        let result = has_changes(&repo_path);
        assert!(result.is_ok());
        assert!(result.unwrap()); // Has untracked file
    }

    #[test]
    fn test_has_changes_nonexistent_repo() {
        let path = PathBuf::from("/nonexistent/path");
        let result = has_changes(&path);
        assert!(result.is_ok());
        assert!(!result.unwrap()); // Returns false for non-repo
    }

    #[test]
    fn test_try_parse_git_command_status() {
        assert!(matches!(
            try_parse_git_command("git status"),
            Some(GitCommand::Status { porcelain: false })
        ));
        assert!(matches!(
            try_parse_git_command("git status --porcelain"),
            Some(GitCommand::Status { porcelain: true })
        ));
        assert!(matches!(
            try_parse_git_command("git status -s"),
            Some(GitCommand::Status { porcelain: true })
        ));
    }

    #[test]
    fn test_try_parse_git_command_branch() {
        assert!(matches!(
            try_parse_git_command("git branch"),
            Some(GitCommand::ListBranches {
                all: false,
                remotes: false
            })
        ));
        assert!(matches!(
            try_parse_git_command("git branch -a"),
            Some(GitCommand::ListBranches {
                all: true,
                remotes: false
            })
        ));
        assert!(matches!(
            try_parse_git_command("git branch -r"),
            Some(GitCommand::ListBranches {
                all: false,
                remotes: true
            })
        ));
    }

    #[test]
    fn test_try_parse_git_command_rev_parse() {
        assert!(matches!(
            try_parse_git_command("git rev-parse HEAD"),
            Some(GitCommand::GetHead)
        ));
        assert!(matches!(
            try_parse_git_command("git rev-parse --abbrev-ref HEAD"),
            Some(GitCommand::GetBranch)
        ));
    }

    #[test]
    fn test_try_parse_git_command_not_interceptable() {
        // Piped commands should not be intercepted by try_parse_git_command
        // (they're handled by parse_command which extracts the git part)
        assert!(try_parse_git_command("git status | grep foo").is_none());
        assert!(try_parse_git_command("git log > log.txt").is_none());

        // Non-git commands should not be intercepted
        assert!(try_parse_git_command("npm test").is_none());
        assert!(try_parse_git_command("echo hello").is_none());

        // Write commands should not be intercepted
        assert!(try_parse_git_command("git commit -m 'message'").is_none());
        assert!(try_parse_git_command("git push origin main").is_none());
        assert!(try_parse_git_command("git checkout -b new-branch").is_none());
    }

    #[test]
    fn test_try_parse_git_command_log() {
        // git log --oneline variants ARE interceptable in Phase 2
        assert!(matches!(
            try_parse_git_command("git log --oneline"),
            Some(GitCommand::LogOneline { count: 10 })
        ));
        assert!(matches!(
            try_parse_git_command("git log --oneline -5"),
            Some(GitCommand::LogOneline { count: 5 })
        ));
        assert!(matches!(
            try_parse_git_command("git log --oneline -n 3"),
            Some(GitCommand::LogOneline { count: 3 })
        ));
        assert!(matches!(
            try_parse_git_command("git log -1 --oneline"),
            Some(GitCommand::LogOneline { count: 1 })
        ));
    }

    #[test]
    fn test_try_parse_git_command_diff() {
        assert!(matches!(
            try_parse_git_command("git diff"),
            Some(GitCommand::Diff { staged: false, .. })
        ));
        assert!(matches!(
            try_parse_git_command("git diff --staged"),
            Some(GitCommand::Diff { staged: true, .. })
        ));
        assert!(matches!(
            try_parse_git_command("git diff --cached"),
            Some(GitCommand::Diff { staged: true, .. })
        ));
        assert!(matches!(
            try_parse_git_command("git diff --name-only"),
            Some(GitCommand::Diff { staged: false, .. })
        ));
    }

    #[test]
    fn test_parse_command_pipes() {
        // Piped commands should be parsed correctly
        let parsed = parse_command("git status | grep modified");
        assert!(matches!(parsed, ParsedCommand::Piped { .. }));

        let parsed = parse_command("git branch | wc -l");
        assert!(matches!(parsed, ParsedCommand::Piped { .. }));

        // Non-interceptable git commands with pipes become Shell
        let parsed = parse_command("git commit -m 'msg' | cat");
        assert!(matches!(parsed, ParsedCommand::Shell(_)));
    }

    #[test]
    fn test_parse_command_redirects() {
        // Redirected commands should be parsed correctly
        let parsed = parse_command("git log --oneline > log.txt");
        assert!(matches!(
            parsed,
            ParsedCommand::Redirect { append: false, .. }
        ));

        let parsed = parse_command("git status >> status.txt");
        assert!(matches!(
            parsed,
            ParsedCommand::Redirect { append: true, .. }
        ));
    }

    #[test]
    fn test_execute_git_command_status() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        // Test status on clean repo
        let result = execute_git_command(&repo_path, &GitCommand::Status { porcelain: true });
        assert!(result.is_ok());
        // Clean repo should have empty porcelain output (no untracked since we committed)
        // But actually we have untracked files in some tests, let's check

        // Add an untracked file
        std::fs::write(repo_path.join("untracked.txt"), "content").unwrap();

        let result = execute_git_command(&repo_path, &GitCommand::Status { porcelain: true });
        assert!(result.is_ok());
        assert!(result.unwrap().contains("untracked.txt"));
    }

    #[test]
    fn test_execute_git_command_branch() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        let result = execute_git_command(
            &repo_path,
            &GitCommand::ListBranches {
                all: false,
                remotes: false,
            },
        );
        assert!(result.is_ok());
        let output = result.unwrap();
        // Should contain the default branch (master or main)
        assert!(output.contains("master") || output.contains("main"));
    }

    #[test]
    fn test_execute_git_command_get_branch() {
        let temp = TempDir::new().unwrap();
        let repo_path = setup_test_repo(&temp);

        let result = execute_git_command(&repo_path, &GitCommand::GetBranch);
        assert!(result.is_ok());
        let output = result.unwrap();
        assert!(output.contains("master") || output.contains("main"));
    }
}
