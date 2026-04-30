//! Integration tests for the commit command.

mod common;

use common::assertions::assert_repo_clean;
use common::fixtures::WorkspaceBuilder;

#[test]
fn test_commit_across_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch (good practice, avoid committing on main)
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/commit-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Create and stage files
    std::fs::write(ws.repo_path("frontend").join("app.js"), "// app").unwrap();
    std::fs::write(ws.repo_path("backend").join("server.rs"), "// server").unwrap();

    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();

    // Commit
    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: add initial files",
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "commit should succeed: {:?}", result.err());

    // Both repos should be clean
    assert_repo_clean(&ws.repo_path("frontend"));
    assert_repo_clean(&ws.repo_path("backend"));
}

#[test]
fn test_commit_skips_no_staged_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let manifest = ws.load_manifest();

    // Only stage changes in app, not lib
    std::fs::write(ws.repo_path("app").join("new.txt"), "content").unwrap();
    std::process::Command::new("git")
        .args(["add", "new.txt"])
        .current_dir(ws.repo_path("app"))
        .output()
        .unwrap();

    // Commit - should only commit in app, skip lib
    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: app only",
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "commit should succeed: {:?}", result.err());

    // app should be clean (committed), lib should still be clean (nothing staged)
    assert_repo_clean(&ws.repo_path("app"));
    assert_repo_clean(&ws.repo_path("lib"));
}

#[test]
fn test_commit_no_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Commit with no staged changes - should succeed (prints "no changes")
    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "empty commit",
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "commit with no changes should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_commit_amend() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create and commit a file
    std::fs::write(ws.repo_path("app").join("file.txt"), "v1").unwrap();
    std::process::Command::new("git")
        .args(["add", "file.txt"])
        .current_dir(ws.repo_path("app"))
        .output()
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "initial",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Modify and stage again
    std::fs::write(ws.repo_path("app").join("file.txt"), "v2").unwrap();
    std::process::Command::new("git")
        .args(["add", "file.txt"])
        .current_dir(ws.repo_path("app"))
        .output()
        .unwrap();

    // Amend
    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "amended",
        true,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "amend should succeed: {:?}", result.err());

    // Verify only 2 commits (initial from fixture + our amended one)
    let output = std::process::Command::new("git")
        .args(["rev-list", "--count", "HEAD"])
        .current_dir(ws.repo_path("app"))
        .output()
        .unwrap();
    let count: usize = String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .unwrap();
    assert_eq!(count, 2, "should have 2 commits (initial + amended)");
}
