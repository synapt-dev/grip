//! Integration tests for the rebase command.

mod common;

use common::fixtures::{write_griptree_config, WorkspaceBuilder};
use common::git_helpers;

#[test]
fn test_rebase_on_default_branch_skips() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let manifest = ws.load_manifest();

    // All repos are on main (default branch) → rebase should skip all and succeed
    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        None,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase on default branch should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_rebase_on_feature_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create a feature branch
    git_helpers::create_branch(&ws.repo_path("app"), "feat/rebase-test");

    // Add a commit on the feature branch
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "feature.txt",
        "feature content",
        "Add feature",
    );

    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        Some("origin/main"),
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase on feature branch should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_rebase_uses_griptree_upstream_mapping() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    git_helpers::create_branch(&ws.repo_path("app"), "dev");
    git_helpers::commit_file(&ws.repo_path("app"), "dev-only.txt", "dev", "Add dev file");
    git_helpers::push_branch(&ws.repo_path("app"), "origin", "dev");
    git_helpers::fetch(&ws.repo_path("app"), "origin", Some("dev"));
    git_helpers::checkout(&ws.repo_path("app"), "main");

    git_helpers::create_branch(&ws.repo_path("app"), "feat/rebase-upstream");
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "feature.txt",
        "feature content",
        "Add feature",
    );

    write_griptree_config(&ws.workspace_root, "feat/griptree", "app", "origin/dev");
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        None,
        true,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase on upstream mapping should succeed: {:?}",
        result.err()
    );

    assert!(ws.repo_path("app").join("dev-only.txt").exists());
    assert!(ws.repo_path("app").join("feature.txt").exists());
}

#[test]
fn test_rebase_abort_no_rebase() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Abort with no rebase in progress → should succeed (no-op)
    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        None,
        false,
        true, // abort
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "abort with no rebase should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_rebase_missing_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Remove the repo directory to simulate a missing repo
    std::fs::remove_dir_all(ws.repo_path("app")).unwrap();

    let manifest = ws.load_manifest();

    // Should gracefully skip missing repos
    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        None,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase with missing repo should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_rebase_invalid_target_keeps_commits() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let repo_path = ws.repo_path("app");
    git_helpers::create_branch(&repo_path, "feat/bad-target");
    git_helpers::commit_file(&repo_path, "feature.txt", "feature", "Add feature");

    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        Some("origin/does-not-exist"),
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase with invalid target should not crash: {:?}",
        result.err()
    );

    assert!(
        git_helpers::log_contains(&repo_path, "Add feature"),
        "expected feature commit to remain after failed rebase"
    );
}

#[test]
fn test_rebase_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();
    let manifest = ws.load_manifest();

    // Corrupt one repo by removing .git
    std::fs::remove_dir_all(ws.repo_path("lib").join(".git")).unwrap();

    // Create a feature branch on the healthy repo
    git_helpers::create_branch(&ws.repo_path("app"), "feat/rebase-healthy");
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "feature.txt",
        "feature",
        "Add feature",
    );

    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        Some("origin/main"),
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "rebase should skip non-git repo without crashing: {:?}",
        result.err()
    );

    assert!(
        git_helpers::log_contains(&ws.repo_path("app"), "Add feature"),
        "expected healthy repo to keep commits"
    );
}
