//! Integration tests for the status command.

mod common;

use common::assertions;
use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_status_clean_workspace() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Should succeed without error on a clean workspace
    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
    );
    assert!(result.is_ok(), "status should succeed: {:?}", result.err());
}

#[test]
fn test_status_with_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create an untracked file in frontend
    std::fs::write(ws.repo_path("frontend").join("new.txt"), "hello").unwrap();

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
    );
    assert!(
        result.is_ok(),
        "status should succeed with changes: {:?}",
        result.err()
    );
}

#[test]
fn test_status_verbose() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        None,
        None,
        false,
    );
    assert!(
        result.is_ok(),
        "verbose status should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_reference_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("main-app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    // Reference repos should appear in status but not cause errors
    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
    );
    assert!(
        result.is_ok(),
        "status with reference repo should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_quiet_mode() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Quiet mode on a clean workspace should succeed (skips clean repos in output)
    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
    );
    assert!(
        result.is_ok(),
        "quiet status should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_quiet_mode_with_changes() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Only frontend has changes
    std::fs::write(ws.repo_path("frontend").join("new.txt"), "hello").unwrap();

    // Quiet mode should succeed - only frontend shown in output
    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
    );
    assert!(
        result.is_ok(),
        "quiet status with changes should succeed: {:?}",
        result.err()
    );
}
