//! End-to-end tests for the diff command.
//!
//! Uses WorkspaceBuilder to create offline workspaces and exercises
//! `run_diff()` with different flags.

mod common;

use common::fixtures::WorkspaceBuilder;

// ── No Changes ──────────────────────────────────────────────────

#[test]
fn test_diff_no_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff on clean workspace should succeed: {:?}",
        result.err()
    );
}

// ── Unstaged Changes ─────────────────────────────────────────────

#[test]
fn test_diff_unstaged_changes() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // Modify a tracked file (README.md was committed by WorkspaceBuilder)
    std::fs::write(ws.repo_path("frontend").join("README.md"), "modified\n").unwrap();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff with unstaged changes should succeed: {:?}",
        result.err()
    );
}

// ── Staged Changes ───────────────────────────────────────────────

#[test]
fn test_diff_staged_changes() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // Modify and stage a tracked file
    let repo_path = ws.repo_path("frontend");
    std::fs::write(repo_path.join("README.md"), "staged content\n").unwrap();

    std::process::Command::new("git")
        .args(["add", "README.md"])
        .current_dir(&repo_path)
        .output()
        .unwrap();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        true, // staged
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff --staged should succeed: {:?}",
        result.err()
    );
}

// ── JSON Output ──────────────────────────────────────────────────

#[test]
fn test_diff_json_output() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // Modify a tracked file
    std::fs::write(ws.repo_path("frontend").join("README.md"), "json diff\n").unwrap();

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
        "diff --json should succeed: {:?}",
        result.err()
    );
}

// ── Multi-Repo Diff ─────────────────────────────────────────────

#[test]
fn test_diff_multi_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Modify files in both repos
    std::fs::write(ws.repo_path("frontend").join("README.md"), "fe changes\n").unwrap();
    std::fs::write(ws.repo_path("backend").join("README.md"), "be changes\n").unwrap();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "multi-repo diff should succeed: {:?}",
        result.err()
    );
}

// ── Diff with Reference Repo ────────────────────────────────────

#[test]
fn test_diff_includes_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("ref-lib")
        .build();

    let manifest = ws.load_manifest();

    // Modify file in reference repo
    std::fs::write(ws.repo_path("ref-lib").join("README.md"), "ref changes\n").unwrap();

    // diff includes reference repos (they're still regular git repos on disk)
    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "diff should succeed: {:?}", result.err());
}
