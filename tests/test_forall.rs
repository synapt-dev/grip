//! Integration tests for the forall command.

mod common;

use common::fixtures::WorkspaceBuilder;

#[test]
fn test_forall_all_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Run a simple command in all repos
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "echo hello",
        false, // sequential
        false, // all repos (changed_only = false)
        false, // intercept enabled
        None,
        None,
    );
    assert!(result.is_ok(), "forall should succeed: {:?}", result.err());
}

#[test]
fn test_forall_changed_only() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Only make changes in frontend
    std::fs::write(ws.repo_path("frontend").join("change.txt"), "data").unwrap();

    // Run with changed_only = true
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "echo changed",
        false,
        true, // changed_only
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "forall changed_only should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_forall_parallel() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .add_repo("gamma")
        .build();

    let manifest = ws.load_manifest();

    // Run in parallel
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "echo parallel",
        true, // parallel
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "forall parallel should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_forall_git_command_intercepted() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Run a git command that should be intercepted (fast path)
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "git rev-parse --abbrev-ref HEAD",
        false,
        false,
        false, // intercept enabled
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "forall intercepted git command should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_forall_no_intercept() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Run a git command with interception disabled
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "git rev-parse --abbrev-ref HEAD",
        false,
        false,
        true, // no_intercept
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "forall no_intercept should succeed: {:?}",
        result.err()
    );
}
