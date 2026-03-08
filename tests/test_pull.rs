//! Integration tests for the pull command.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use gitgrip::git::open_repo;
use gitgrip::git::remote::{safe_pull_latest_with_mode, PullMode};

#[tokio::test]
async fn test_pull_merge_on_clean_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::pull::run_pull(
        &ws.workspace_root,
        &manifest,
        false,
        None,
        None,
        true,
        true,
    )
    .await;

    assert!(
        result.is_ok(),
        "pull should succeed on clean repo: {:?}",
        result.err()
    );
}

#[test]
fn test_safe_pull_fetches_when_no_upstream() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let repo_path = ws.repo_path("app");

    git_helpers::create_branch(&repo_path, "feature/no-upstream");

    let repo = open_repo(&repo_path).expect("open repo");
    let result = safe_pull_latest_with_mode(&repo, "main", "origin", PullMode::Merge)
        .expect("safe pull should succeed");

    assert!(result.pulled, "expected fetch to count as pulled");
    assert_eq!(result.message.as_deref(), Some("fetched (no upstream)"));
}

#[test]
fn test_safe_pull_reports_missing_remote() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let repo_path = ws.repo_path("app");

    git_helpers::remove_remote(&repo_path, "origin");

    let repo = open_repo(&repo_path).expect("open repo");
    let result = safe_pull_latest_with_mode(&repo, "main", "origin", PullMode::Merge)
        .expect("safe pull should return a result");

    assert!(!result.pulled, "expected pull to fail without a remote");
    assert!(
        result.message.as_ref().is_some_and(|msg| !msg.is_empty()),
        "expected an error message when the remote is missing"
    );
}

#[test]
fn test_safe_pull_nondefault_missing_remote_errors() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let repo_path = ws.repo_path("app");

    git_helpers::create_branch(&repo_path, "feature-no-remote");
    git_helpers::remove_remote(&repo_path, "origin");

    let repo = open_repo(&repo_path).expect("open repo");
    let result = safe_pull_latest_with_mode(&repo, "main", "origin", PullMode::Merge);

    assert!(
        result.is_err(),
        "expected safe pull to error when remote is missing on a feature branch"
    );
}
