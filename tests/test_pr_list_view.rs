//! Integration tests for pr list and pr view commands.
//!
//! These test the basic flow: repos on default branch are handled,
//! reference repos are filtered, and the commands handle API errors gracefully.

mod common;

use common::fixtures::WorkspaceBuilder;

// ── pr list ───────────────────────────────────────────────────

#[tokio::test]
async fn test_pr_list_all_on_default_branch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let manifest = ws.load_manifest();

    // list_pull_requests calls platform API which fails for file:// URLs,
    // but the command should handle errors gracefully
    let result = gitgrip::cli::commands::pr::run_pr_list(
        &ws.workspace_root,
        &manifest,
        gitgrip::cli::args::PrStateFilter::Open,
        None,
        30,
        true, // json
    )
    .await;

    assert!(
        result.is_ok(),
        "pr list should handle API errors: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_list_repo_filter_not_found() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_list(
        &ws.workspace_root,
        &manifest,
        gitgrip::cli::args::PrStateFilter::Open,
        Some("nonexistent"),
        30,
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
async fn test_pr_list_skips_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pr::run_pr_list(
        &ws.workspace_root,
        &manifest,
        gitgrip::cli::args::PrStateFilter::Open,
        None,
        30,
        true,
    )
    .await;

    assert!(
        result.is_ok(),
        "pr list should skip reference repos: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_list_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    std::fs::remove_dir_all(ws.repo_path("app").join(".git")).unwrap();

    let result = gitgrip::cli::commands::pr::run_pr_list(
        &ws.workspace_root,
        &manifest,
        gitgrip::cli::args::PrStateFilter::Open,
        None,
        30,
        true,
    )
    .await;

    assert!(
        result.is_ok(),
        "pr list should skip non-git repos: {:?}",
        result.err()
    );
}

// ── pr view ───────────────────────────────────────────────────

#[tokio::test]
async fn test_pr_view_all_on_default_branch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::pr::run_pr_view(gitgrip::cli::commands::pr::PRViewOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            number: None,
            repo_filter: None,
            json_output: true,
        })
        .await;

    assert!(
        result.is_ok(),
        "pr view should succeed when all on default branch: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_view_repo_filter_not_found() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::pr::run_pr_view(gitgrip::cli::commands::pr::PRViewOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            number: Some(1),
            repo_filter: Some("nonexistent"),
            json_output: true,
        })
        .await;

    assert!(result.is_err(), "should error for unknown repo");
}

#[tokio::test]
async fn test_pr_view_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    std::fs::remove_dir_all(ws.repo_path("app").join(".git")).unwrap();

    let result =
        gitgrip::cli::commands::pr::run_pr_view(gitgrip::cli::commands::pr::PRViewOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            number: None,
            repo_filter: None,
            json_output: true,
        })
        .await;

    assert!(
        result.is_ok(),
        "pr view should skip non-git repos: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_pr_view_skips_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();
    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::pr::run_pr_view(gitgrip::cli::commands::pr::PRViewOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            number: None,
            repo_filter: None,
            json_output: true,
        })
        .await;

    assert!(
        result.is_ok(),
        "pr view should skip reference repos: {:?}",
        result.err()
    );
}
