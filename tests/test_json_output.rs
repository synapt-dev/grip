//! Integration tests for JSON output mode (Phase 6).
//!
//! Verifies that the `--json` flag on status, diff, and branch commands
//! succeeds and produces output without errors.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

// ── status --json ──────────────────────────────────────────────

#[test]
fn test_status_json_clean() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        true, // json
    );
    assert!(
        result.is_ok(),
        "status json on clean workspace should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_json_with_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create an untracked file
    std::fs::write(ws.repo_path("frontend").join("untracked.txt"), "test").unwrap();

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        true, // json
    );
    assert!(
        result.is_ok(),
        "status json with changes should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_json_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        true, // json
    );
    assert!(
        result.is_ok(),
        "status json with reference repos should succeed: {:?}",
        result.err()
    );
}

// ── diff --json ──────────────────────────────────────────────

#[test]
fn test_diff_json_no_changes() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        true, // json
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff json no changes should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_diff_json_with_changes() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // Modify an existing tracked file
    std::fs::write(ws.repo_path("frontend").join("README.md"), "modified\n").unwrap();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        true, // json
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff json with changes should succeed: {:?}",
        result.err()
    );
}

// ── branch --json ──────────────────────────────────────────────

#[test]
fn test_branch_json_list() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create a branch first
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();
    git_helpers::checkout(&ws.repo_path("app"), "main");

    // List branches in JSON mode
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: None,
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: true,
        });
    assert!(
        result.is_ok(),
        "branch json list should succeed: {:?}",
        result.err()
    );
}
