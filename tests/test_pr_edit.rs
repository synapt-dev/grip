//! Command-orchestration regression for `gr pr edit`'s multi-repo scope guard
//! (grip#771, grip#773).
//!
//! Same reasoning as `test_pr_review.rs`: the committed guard-unit tests all
//! call `require_explicit_multi_repo_scope` directly, so nothing in the
//! original suite would notice the guard's call-site being removed from
//! `run_pr_edit`'s own body. These tests exercise `run_pr_edit` end to end
//! and assert on the wiremock server's received-request log.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use common::mock_platform::{
    mock_list_prs, mock_update_pull_request, point_repo_at_mock, setup_github_mock,
};
use wiremock::http::Method;

// ── grip#771 exact shape: unrelated repo, unscoped edit, zero PATCHes ─────

#[tokio::test]
async fn test_pr_edit_unscoped_multi_match_sends_zero_update_patches() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new()
        .add_repo("research")
        .add_repo("recall")
        .build();
    let mut manifest = ws.load_manifest();

    for repo_name in ["research", "recall"] {
        git_helpers::create_branch(
            &ws.repo_path(repo_name),
            "test/a7-b3-corroborate-content-discard",
        );
        git_helpers::commit_file(&ws.repo_path(repo_name), "f.txt", "x", "Add f");
        point_repo_at_mock(&mut manifest, repo_name, &server);
    }

    mock_list_prs(
        &server,
        vec![(892, "test/a7-b3-corroborate-content-discard")],
    )
    .await;

    let result = gitgrip::cli::commands::pr::run_pr_edit(
        &ws.workspace_root,
        &manifest,
        Some("a title meant for exactly one PR"),
        Some("a body meant for exactly one PR"),
        None,  // no --repo
        false, // no --all
        false,
    )
    .await;

    assert!(
        result.is_err(),
        "unscoped 2-repo match must be refused, not silently edited"
    );

    let requests = server.received_requests().await.unwrap();
    let update_patches: Vec<_> = requests
        .iter()
        .filter(|r| r.method == Method::PATCH && r.url.path().contains("/pulls/"))
        .collect();
    assert!(
        update_patches.is_empty(),
        "expected zero PR-update PATCHes, got {}: {:?}",
        update_patches.len(),
        update_patches
            .iter()
            .map(|r| r.url.path())
            .collect::<Vec<_>>()
    );
}

// ── --repo explicitly scopes past the guard, edit proceeds normally ───────

#[tokio::test]
async fn test_pr_edit_explicit_repo_filter_proceeds_and_patches() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();

    for repo_name in ["frontend", "backend"] {
        git_helpers::create_branch(&ws.repo_path(repo_name), "feat/test");
        git_helpers::commit_file(&ws.repo_path(repo_name), "f.txt", "x", "Add f");
        point_repo_at_mock(&mut manifest, repo_name, &server);
    }

    mock_list_prs(&server, vec![(1, "feat/test")]).await;
    mock_update_pull_request(&server, 1).await;

    let result = gitgrip::cli::commands::pr::run_pr_edit(
        &ws.workspace_root,
        &manifest,
        Some("scoped title"),
        Some("scoped body"),
        Some(&["frontend".to_string()]), // explicit --repo
        false,
        false,
    )
    .await;

    assert!(
        result.is_ok(),
        "explicit --repo scoping a single match should proceed: {:?}",
        result.err()
    );

    let requests = server.received_requests().await.unwrap();
    let update_patches = requests
        .iter()
        .filter(|r| r.method == Method::PATCH && r.url.path().contains("/pulls/"))
        .count();
    assert_eq!(
        update_patches, 1,
        "expected exactly one update PATCH once explicitly scoped"
    );
}
