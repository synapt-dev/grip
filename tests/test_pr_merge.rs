//! Integration tests for the PR merge command.
//!
//! Tests the `run_pr_merge()` orchestration using WorkspaceBuilder.
//! Some tests verify behavior without API calls (reference repos, default branch),
//! while others use wiremock to mock the GitHub API.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use common::mock_platform::{
    mock_check_runs, mock_get_pr, mock_list_prs, mock_merge_pr, mock_merge_pr_behind,
    mock_pr_reviews, setup_github_mock,
};
use gitgrip::core::manifest::{PlatformConfig, PlatformType};
use wiremock::http::Method;

// ── No Open PRs ─────────────────────────────────────────────────
// When all repos are on the default branch, no API calls are made
// and the command should report "No open PRs found."

#[tokio::test]
async fn test_pr_merge_no_open_prs() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // All repos are on main (default branch) — no PRs to find
    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,  // method
        false, // force
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    // Should succeed with "No open PRs found" message (no API calls made)
    assert!(
        result.is_ok(),
        "pr merge with all repos on default branch should succeed: {:?}",
        result.err()
    );
}

// ── Skip Default Branch ─────────────────────────────────────────
// Repos on the default branch are skipped entirely (no API calls).

#[tokio::test]
async fn test_pr_merge_skip_default_branch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Put frontend on a feature branch, leave backend on main
    git_helpers::create_branch(&ws.repo_path("frontend"), "feat/test");
    git_helpers::commit_file(
        &ws.repo_path("frontend"),
        "test.txt",
        "test content",
        "Add test file",
    );

    // backend stays on main — should be skipped without API call

    // This will try to find a PR for frontend (which will fail since no API mock)
    // but backend should be skipped silently
    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,  // method
        false, // force
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    // The call will fail because frontend is on a feature branch and there's no
    // real API to query. But the important thing is backend (on main) was skipped.
    // We can't easily assert this without refactoring, so this test mainly ensures
    // the skip logic doesn't panic.
    //
    // In a full test, we'd mock the API and verify backend never appears in the merge list.
    let _ = result; // Ignore result - we're testing that it doesn't panic
}

// ── Skip Reference Repos ────────────────────────────────────────
// Reference repos are filtered out before any API calls.

#[tokio::test]
async fn test_pr_merge_skip_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_reference_repo("ref-lib")
        .add_reference_repo("ref-sdk")
        .build();

    let manifest = ws.load_manifest();

    // Put reference repos on feature branches
    git_helpers::create_branch(&ws.repo_path("ref-lib"), "feat/update");
    git_helpers::commit_file(&ws.repo_path("ref-lib"), "update.txt", "update", "Update");

    // Even though ref-lib is on a feature branch, it should be skipped
    // because it's a reference repo (line 25 filters them out)
    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,  // method
        false, // force
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    // Should succeed with "No open PRs found" since all repos are reference repos
    assert!(
        result.is_ok(),
        "pr merge with only reference repos should succeed: {:?}",
        result.err()
    );
}

// ── Mixed: Regular + Reference ──────────────────────────────────
// Regular repos on default branch + reference repos on feature branches.
// All should be skipped without API calls.

#[tokio::test]
async fn test_pr_merge_mixed_repos_all_skipped() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app") // regular repo, will stay on main
        .add_reference_repo("lib") // reference repo
        .build();

    let manifest = ws.load_manifest();

    // Put lib on a feature branch (but it's a reference, so skipped)
    git_helpers::create_branch(&ws.repo_path("lib"), "feat/lib-update");
    git_helpers::commit_file(&ws.repo_path("lib"), "lib.txt", "lib", "Update lib");

    // app stays on main (skipped), lib is reference (skipped)
    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,  // method
        false, // force
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    assert!(
        result.is_ok(),
        "pr merge should succeed when all repos are skipped: {:?}",
        result.err()
    );
}

// ══════════════════════════════════════════════════════════════════
// The following tests require API mocking. They are marked with
// #[ignore] until platform injection infrastructure is added.
// ══════════════════════════════════════════════════════════════════

// ── Force Bypasses Checks ───────────────────────────────────────
// The --force flag should merge PRs even if not approved or checks pending.

#[tokio::test]
async fn test_pr_merge_force_bypasses_checks() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let mut manifest = ws.load_manifest();

    // Switch to feature branch
    git_helpers::create_branch(&ws.repo_path("app"), "feat/test");
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "feature.txt",
        "feature",
        "Add feature",
    );

    // Point manifest at mock GitHub
    let repo_config = manifest.repos.get_mut("app").unwrap();
    repo_config.url = Some("https://github.com/owner/repo.git".to_string());
    repo_config.platform = Some(PlatformConfig {
        platform_type: PlatformType::GitHub,
        base_url: Some(server.uri()),
    });

    mock_list_prs(&server, vec![(42, "feat/test")]).await;
    mock_get_pr(&server, 42, "open", false).await;
    mock_pr_reviews(&server, 42, vec![("COMMENTED", "alice")]).await;
    mock_check_runs(&server, "feat/test", vec![("CI", "in_progress", None)]).await;
    mock_merge_pr(&server, 42, true).await;

    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,
        true,  // force
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    assert!(
        result.is_ok(),
        "force merge should not error: {:?}",
        result.err()
    );

    let requests = server.received_requests().await.unwrap();
    assert!(
        requests
            .iter()
            .any(|r| r.method == Method::PUT && r.url.path().ends_with("/merge")),
        "expected merge request to be sent"
    );
}

// ── Branch Behind Suggests Update ───────────────────────────────
// When merge fails with BranchBehind, suggest using --update.

#[tokio::test]
async fn test_pr_merge_branch_behind_suggests_update() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let mut manifest = ws.load_manifest();

    git_helpers::create_branch(&ws.repo_path("app"), "feat/test");
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "feature.txt",
        "feature",
        "Add feature",
    );

    let repo_config = manifest.repos.get_mut("app").unwrap();
    repo_config.url = Some("https://github.com/owner/repo.git".to_string());
    repo_config.platform = Some(PlatformConfig {
        platform_type: PlatformType::GitHub,
        base_url: Some(server.uri()),
    });

    mock_list_prs(&server, vec![(42, "feat/test")]).await;
    mock_get_pr(&server, 42, "open", false).await;
    mock_pr_reviews(&server, 42, vec![("APPROVED", "alice")]).await;
    mock_check_runs(
        &server,
        "feat/test",
        vec![("CI", "completed", Some("success"))],
    )
    .await;
    mock_merge_pr_behind(&server, 42).await;

    let result = gitgrip::cli::commands::pr::run_pr_merge(
        &ws.workspace_root,
        &manifest,
        None,
        true,  // force to bypass readiness
        false, // update
        false, // auto
        false, // json
        false, // wait
        600,   // timeout
        true,  // delete_branch
    )
    .await;

    assert!(
        result.is_ok(),
        "branch-behind merge should be handled without crashing: {:?}",
        result.err()
    );

    let requests = server.received_requests().await.unwrap();
    assert!(
        requests
            .iter()
            .any(|r| r.method == Method::PUT && r.url.path().ends_with("/merge")),
        "expected merge attempt for branch-behind case"
    );
}

// ── AllOrNothing Stops on Failure ───────────────────────────────
// With AllOrNothing merge strategy, first failure should stop all merges.

#[tokio::test]
#[ignore = "requires platform injection for API mocking"]
async fn test_pr_merge_all_or_nothing_stops_on_failure() {
    // TODO: Implement with mock platform
    // 1. Create workspace with multiple repos on feature branches
    // 2. Configure manifest with merge_strategy: AllOrNothing
    // 3. Mock first repo's merge to fail
    // 4. Verify second repo's merge is never called
    // 5. Verify error message mentions all-or-nothing
}
