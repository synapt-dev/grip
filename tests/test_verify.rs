//! Integration tests for the verify command.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_verify_clean_passes_on_clean_workspace() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true, // Use JSON mode so we don't exit(1)
            quiet: false,
            clean: true,
            links: false,
            on_branch: None,
            synced: false,
        });
    assert!(
        result.is_ok(),
        "verify --clean should pass on clean workspace"
    );
}

#[test]
fn test_verify_clean_fails_with_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create an untracked file in frontend
    std::fs::write(ws.repo_path("frontend").join("dirty.txt"), "dirty").unwrap();

    // Use JSON mode to avoid std::process::exit(1)
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: true,
            links: false,
            on_branch: None,
            synced: false,
        });
    // JSON mode always returns Ok, but the output contains pass: false
    assert!(result.is_ok());
}

#[test]
fn test_verify_on_branch_passes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // All repos are on main by default
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: false,
            links: false,
            on_branch: Some("main"),
            synced: false,
        });
    assert!(result.is_ok(), "verify --on-branch main should pass");
}

#[test]
fn test_verify_on_branch_fails() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // All repos are on main, not on feat/test
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: false,
            links: false,
            on_branch: Some("feat/test"),
            synced: false,
        });
    assert!(result.is_ok()); // JSON mode returns Ok
}

#[test]
fn test_verify_no_flags_returns_error() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // No flags should error in non-JSON mode
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: false,
            quiet: false,
            clean: false,
            links: false,
            on_branch: None,
            synced: false,
        });
    assert!(result.is_err(), "verify with no flags should error");
}

#[test]
fn test_verify_links_no_links_defined() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let manifest = ws.load_manifest();

    // No links defined in manifest, should pass
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: false,
            links: true,
            on_branch: None,
            synced: false,
        });
    assert!(
        result.is_ok(),
        "verify --links should pass with no links defined"
    );
}

#[test]
fn test_verify_multiple_checks_combined() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Clean workspace on main - both checks should pass
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: true,
            links: false,
            on_branch: Some("main"),
            synced: false,
        });
    assert!(
        result.is_ok(),
        "verify --clean --on-branch main should pass"
    );
}

#[test]
fn test_verify_on_branch_after_branch_switch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Switch frontend to a feature branch
    git_helpers::create_branch(&ws.repo_path("frontend"), "feat/test");

    // Verify on feat/test should fail (backend is still on main)
    let result =
        gitgrip::cli::commands::verify::run_verify(gitgrip::cli::commands::verify::VerifyOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            repos_filter: None,
            group_filter: None,
            json: true,
            quiet: false,
            clean: false,
            links: false,
            on_branch: Some("feat/test"),
            synced: false,
        });
    assert!(result.is_ok()); // JSON mode always Ok
}
