//! Integration tests for pr checks and pr status commands.
//!
//! These test the basic flow: repos on default branch are skipped,
//! reference repos are filtered, and the commands don't panic.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

// ── pr checks ───────────────────────────────────────────────────

#[tokio::test]
async fn test_pr_checks_all_on_default_branch() {
    // When all repos are on their default branch, pr checks should
    // skip all of them and succeed without API calls.
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_checks(
        &ws.workspace_root,
        &manifest,
        None, // no repo filter
        true, // json output to avoid terminal formatting issues in tests
    )
    .await;

    assert!(
        result.is_ok(),
        "pr checks should succeed when all on default branch: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_checks_json_output_empty() {
    // With all repos on default branch, JSON output should be an empty array
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_checks(
        &ws.workspace_root,
        &manifest,
        None, // no repo filter
        true, // json
    )
    .await;

    assert!(result.is_ok());
}

#[tokio::test]
async fn test_pr_checks_handles_api_error_on_feature_branch() {
    // Feature branch with file:// remote triggers platform API error;
    // the command should handle it and return Ok.
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    git_helpers::create_branch(&ws.repo_path("app"), "feat/checks-error");

    let result = gitgrip::cli::commands::pr::run_pr_checks(
        &ws.workspace_root,
        &manifest,
        None, // no repo filter
        true, // json
    )
    .await;

    assert!(
        result.is_ok(),
        "pr checks should handle API errors: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_checks_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    std::fs::remove_dir_all(ws.repo_path("app").join(".git")).unwrap();

    let result = gitgrip::cli::commands::pr::run_pr_checks(
        &ws.workspace_root,
        &manifest,
        None, // no repo filter
        true, // json
    )
    .await;

    assert!(
        result.is_ok(),
        "pr checks should skip non-git repos: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_checks_repo_filter_not_found() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_checks(
        &ws.workspace_root,
        &manifest,
        Some("nonexistent"),
        true,
    )
    .await;

    assert!(result.is_err(), "should error for unknown repo");
    let err = result.unwrap_err().to_string();
    assert!(
        err.contains("not found"),
        "error should mention 'not found': {err}"
    );
}

#[tokio::test]
async fn test_pr_checks_repo_filter_valid() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();
    let manifest = ws.load_manifest();

    // Filter to just "app" — should succeed (all on default branch, so no API calls)
    let result =
        gitgrip::cli::commands::pr::run_pr_checks(&ws.workspace_root, &manifest, Some("app"), true)
            .await;

    assert!(
        result.is_ok(),
        "repo filter to valid repo should succeed: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_checks_skips_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();
    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::pr::run_pr_checks(&ws.workspace_root, &manifest, None, true).await;

    assert!(
        result.is_ok(),
        "pr checks should skip reference repos: {:?}",
        result.err()
    );
}

// ── pr status ───────────────────────────────────────────────────

#[tokio::test]
async fn test_pr_status_all_on_default_branch() {
    // When all repos are on default branch, pr status should skip them.
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_status(
        &ws.workspace_root,
        &manifest,
        true, // json
    )
    .await;

    assert!(
        result.is_ok(),
        "pr status should succeed when all on default branch: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_status_skips_reference_repos() {
    // Reference repos should be filtered out of pr status.
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();
    let manifest = ws.load_manifest();

    // Put app on a feature branch
    git_helpers::create_branch(&ws.repo_path("app"), "feat/test");

    let result = gitgrip::cli::commands::pr::run_pr_status(
        &ws.workspace_root,
        &manifest,
        true, // json
    )
    .await;

    // Should succeed even though the platform API will fail for file:// URLs
    // because the reference repo is filtered and app's API failure is handled
    assert!(
        result.is_ok(),
        "pr status should handle reference repos: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_status_non_json_no_changes() {
    // Test non-JSON output mode when no repos have feature branches
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_status(
        &ws.workspace_root,
        &manifest,
        false, // human-readable output
    )
    .await;

    assert!(
        result.is_ok(),
        "pr status (non-json) should succeed: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_status_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    std::fs::remove_dir_all(ws.repo_path("app").join(".git")).unwrap();

    let result = gitgrip::cli::commands::pr::run_pr_status(
        &ws.workspace_root,
        &manifest,
        true, // json
    )
    .await;

    assert!(
        result.is_ok(),
        "pr status should skip non-git repos: {:?}",
        result.err()
    );
}
