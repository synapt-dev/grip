//! Diff command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{get_manifest_repo_info, RepoInfo};
use crate::git::{open_repo, path_exists};
use git2::{DiffOptions, Repository};
use std::path::PathBuf;

/// Run the diff command
pub fn run_diff(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    staged: bool,
    json: bool,
) -> anyhow::Result<()> {
    let mut repos: Vec<RepoInfo> = manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(name, config, workspace_root, &manifest.settings)
        })
        .collect();

    // Include manifest repo in diff operations
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        repos.push(manifest_repo);
    }

    if json {
        return run_diff_json(workspace_root, &repos, staged);
    }

    let mut has_changes = false;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let diff_output = get_diff(&git_repo, staged)?;
                if !diff_output.is_empty() {
                    if has_changes {
                        println!();
                    }
                    Output::header(&format!("diff: {}", repo.name));
                    println!("{}", diff_output);
                    has_changes = true;
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    if !has_changes {
        println!("No changes.");
    }

    Ok(())
}

/// Run diff in JSON mode
fn run_diff_json(
    _workspace_root: &PathBuf,
    repos: &[RepoInfo],
    staged: bool,
) -> anyhow::Result<()> {
    #[derive(serde::Serialize)]
    struct JsonDiff {
        repo: String,
        diff: String,
        files_changed: usize,
    }

    let mut results: Vec<JsonDiff> = Vec::new();

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let diff_output = get_diff(&git_repo, staged)?;
                if !diff_output.is_empty() {
                    // Count files changed by counting "diff --git" headers
                    let files_changed = diff_output
                        .lines()
                        .filter(|l| l.starts_with("diff --git"))
                        .count();
                    let files_changed = if files_changed == 0 { 1 } else { files_changed };
                    results.push(JsonDiff {
                        repo: repo.name.clone(),
                        diff: diff_output,
                        files_changed,
                    });
                }
            }
            Err(_) => continue,
        }
    }

    println!("{}", serde_json::to_string_pretty(&results)?);
    Ok(())
}

/// Get diff output for a repository
fn get_diff(repo: &Repository, staged: bool) -> anyhow::Result<String> {
    let mut output = String::new();
    let mut opts = DiffOptions::new();

    let diff = if staged {
        // Diff between HEAD and index (staged changes)
        let head = repo.head()?.peel_to_tree()?;
        repo.diff_tree_to_index(Some(&head), None, Some(&mut opts))?
    } else {
        // Diff between index and workdir (unstaged changes)
        repo.diff_index_to_workdir(None, Some(&mut opts))?
    };

    diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
        let prefix = match line.origin() {
            '+' => "+",
            '-' => "-",
            ' ' => " ",
            '>' => ">",
            '<' => "<",
            'F' => "", // File header
            'H' => "", // Hunk header
            'B' => "", // Binary
            _ => "",
        };

        // Color the output
        let content = std::str::from_utf8(line.content()).unwrap_or("");
        let colored_line = match line.origin() {
            '+' => format!("\x1b[32m{}{}\x1b[0m", prefix, content.trim_end()),
            '-' => format!("\x1b[31m{}{}\x1b[0m", prefix, content.trim_end()),
            '@' => format!("\x1b[36m{}\x1b[0m", content.trim_end()),
            _ => format!("{}{}", prefix, content.trim_end()),
        };

        output.push_str(&colored_line);
        output.push('\n');
        true
    })?;

    Ok(output)
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

        // Create initial file and commit
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "initial content\n").unwrap();

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
    fn test_diff_unstaged_changes() {
        let (temp_dir, repo) = setup_test_repo();

        // Modify the file (unstaged)
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "modified content\n").unwrap();

        let diff_output = get_diff(&repo, false).unwrap();
        assert!(diff_output.contains("-initial content"));
        assert!(diff_output.contains("+modified content"));
    }

    #[test]
    fn test_diff_staged_changes() {
        let (temp_dir, repo) = setup_test_repo();

        // Modify and stage the file
        let file_path = temp_dir.path().join("test.txt");
        fs::write(&file_path, "staged content\n").unwrap();

        {
            let mut index = repo.index().unwrap();
            index.add_path(std::path::Path::new("test.txt")).unwrap();
            index.write().unwrap();
        }

        let diff_output = get_diff(&repo, true).unwrap();
        assert!(diff_output.contains("-initial content"));
        assert!(diff_output.contains("+staged content"));
    }

    #[test]
    fn test_diff_no_changes() {
        let (_temp_dir, repo) = setup_test_repo();
        let diff_output = get_diff(&repo, false).unwrap();
        assert!(diff_output.is_empty());
    }
}
