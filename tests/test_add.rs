//! Integration tests for the add command.

mod common;

use common::fixtures::WorkspaceBuilder;

#[test]
fn test_add_all() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create untracked files in both repos
    std::fs::write(ws.repo_path("frontend").join("new.txt"), "hello").unwrap();
    std::fs::write(ws.repo_path("backend").join("other.txt"), "world").unwrap();

    let files = vec![".".to_string()];
    let result =
        gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None);
    assert!(result.is_ok(), "add should succeed: {:?}", result.err());

    // Verify files are staged (check with git diff --cached)
    let output = std::process::Command::new("git")
        .args(["diff", "--cached", "--name-only"])
        .current_dir(ws.repo_path("frontend"))
        .output()
        .unwrap();
    let staged = String::from_utf8_lossy(&output.stdout);
    assert!(staged.contains("new.txt"), "new.txt should be staged");

    let output = std::process::Command::new("git")
        .args(["diff", "--cached", "--name-only"])
        .current_dir(ws.repo_path("backend"))
        .output()
        .unwrap();
    let staged = String::from_utf8_lossy(&output.stdout);
    assert!(staged.contains("other.txt"), "other.txt should be staged");
}

#[test]
fn test_add_no_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Add with no changes -- should succeed silently
    let files = vec![".".to_string()];
    let result =
        gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None);
    assert!(
        result.is_ok(),
        "add with no changes should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_add_specific_file() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create two files but only add one
    std::fs::write(ws.repo_path("app").join("include.txt"), "yes").unwrap();
    std::fs::write(ws.repo_path("app").join("exclude.txt"), "no").unwrap();

    let files = vec!["include.txt".to_string()];
    let result =
        gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None);
    assert!(
        result.is_ok(),
        "add specific file should succeed: {:?}",
        result.err()
    );
}
