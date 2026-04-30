//! Integration tests for the branch command.

mod common;

use common::assertions::{assert_branch_exists, assert_branch_not_exists, assert_on_branch};
use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_branch_create_across_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/new-feature"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch create should succeed: {:?}",
        result.err()
    );

    // Both repos should now be on the new branch
    assert_on_branch(&ws.repo_path("frontend"), "feat/new-feature");
    assert_on_branch(&ws.repo_path("backend"), "feat/new-feature");
}

#[test]
fn test_branch_delete() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch first
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/to-delete"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Switch back to main so we can delete
    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();

    // Delete the branch
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/to-delete"),
            delete: true,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch delete should succeed: {:?}",
        result.err()
    );

    assert_branch_not_exists(&ws.repo_path("frontend"), "feat/to-delete");
    assert_branch_not_exists(&ws.repo_path("backend"), "feat/to-delete");
}

#[test]
fn test_branch_list() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create a couple branches
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/one"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();
    git_helpers::checkout(&ws.repo_path("app"), "main");
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/two"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // List branches (no name passed)
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: None,
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch list should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_filter_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .add_repo("shared")
        .build();

    let manifest = ws.load_manifest();

    // Create branch only in frontend and backend
    let filter = vec!["frontend".to_string(), "backend".to_string()];
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/filtered"),
            delete: false,
            move_commits: false,
            repos_filter: Some(&filter),
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "filtered branch should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("frontend"), "feat/filtered");
    assert_on_branch(&ws.repo_path("backend"), "feat/filtered");
    // shared should still be on main
    assert_on_branch(&ws.repo_path("shared"), "main");
}

#[test]
fn test_branch_skip_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/skip-refs"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(result.is_ok(), "branch should succeed: {:?}", result.err());

    // app should be on the new branch
    assert_on_branch(&ws.repo_path("app"), "feat/skip-refs");
    // docs (reference) should still be on main
    assert_on_branch(&ws.repo_path("docs"), "main");
}

#[test]
fn test_branch_idempotent_creation() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/existing"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Create same branch again -- should not error (prints "already exists")
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/existing"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "creating an existing branch should not fail: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_not_cloned_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Manually remove the cloned repo to simulate "not cloned"
    std::fs::remove_dir_all(ws.repo_path("app")).unwrap();

    let manifest = ws.load_manifest();

    // Should succeed (prints warning for not-cloned repo)
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/no-repo"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch on missing repo should not fail: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_create_then_verify_branches_exist() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();

    let manifest = ws.load_manifest();

    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/verify"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    assert_branch_exists(&ws.repo_path("alpha"), "feat/verify");
    assert_branch_exists(&ws.repo_path("beta"), "feat/verify");
    // main should still exist too
    assert_branch_exists(&ws.repo_path("alpha"), "main");
    assert_branch_exists(&ws.repo_path("beta"), "main");
}

/// Regression test for grip#401: `gr branch` on an existing branch must
/// switch to it so subsequent commits land on the correct branch.
#[test]
fn test_branch_switches_to_existing_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Create feat/target and then switch back to main
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/target"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();
    assert_on_branch(&ws.repo_path("app"), "main");

    // Run `gr branch feat/target` again — should switch to it
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/target"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Must be on feat/target, not main
    assert_on_branch(&ws.repo_path("app"), "feat/target");
}
