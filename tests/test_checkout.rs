//! Integration tests for the checkout command.

mod common;

use common::assertions::assert_on_branch;
use common::fixtures::WorkspaceBuilder;

#[test]
fn test_checkout_existing_branch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create a branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/checkout-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Go back to main
    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();
    assert_on_branch(&ws.repo_path("frontend"), "main");
    assert_on_branch(&ws.repo_path("backend"), "main");

    // Checkout the feature branch
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/checkout-test",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("frontend"), "feat/checkout-test");
    assert_on_branch(&ws.repo_path("backend"), "feat/checkout-test");
}

#[test]
fn test_checkout_nonexistent_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Checkout a branch that doesn't exist -- should succeed (skips repos)
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/does-not-exist",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout of nonexistent branch should not error: {:?}",
        result.err()
    );

    // Should still be on main
    assert_on_branch(&ws.repo_path("app"), "main");
}

#[test]
fn test_checkout_main() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let manifest = ws.load_manifest();

    // Create and switch to feature branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/temp"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();
    assert_on_branch(&ws.repo_path("app"), "feat/temp");

    // Checkout main
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout main should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("app"), "main");
    assert_on_branch(&ws.repo_path("lib"), "main");
}

#[test]
fn test_checkout_create_flag() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Use -b flag to create and checkout in one command
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/new-feature",
        true, // create = true (-b flag)
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout -b should succeed: {:?}",
        result.err()
    );

    // Both repos should now be on the new branch
    assert_on_branch(&ws.repo_path("frontend"), "feat/new-feature");
    assert_on_branch(&ws.repo_path("backend"), "feat/new-feature");
}
#[test]
fn test_checkout_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch across repos
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/checkout-safe"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Corrupt backend repo by removing .git
    std::fs::remove_dir_all(ws.repo_path("backend").join(".git")).unwrap();

    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/checkout-safe",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout should not crash on non-git repo: {:?}",
        result.err()
    );

    // Healthy repo should switch; corrupted repo remains non-git
    assert_on_branch(&ws.repo_path("frontend"), "feat/checkout-safe");
    assert!(!ws.repo_path("backend").join(".git").exists());
}
