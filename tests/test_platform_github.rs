//! Integration tests for the GitHub platform adapter using wiremock.
//!
//! Tests the GitHubAdapter against mock HTTP responses, verifying correct
//! API interaction without requiring real GitHub credentials or network access.

mod common;

use common::mock_platform::*;
use gitgrip::platform::traits::HostingPlatform;
use gitgrip::platform::{CheckState, MergeMethod};

// ── PR Create ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_github_create_pr() {
    let (server, adapter) = setup_github_mock().await;
    mock_create_pr(&server, 42, "https://github.com/owner/repo/pull/42").await;

    let result = adapter
        .create_pull_request("owner", "repo", "feat/test", "main", "Test PR", None, false)
        .await;

    assert!(result.is_ok(), "create PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert_eq!(pr.number, 42);
    assert_eq!(pr.url, "https://github.com/owner/repo/pull/42");
}

#[tokio::test]
async fn test_github_create_pr_with_body_and_draft() {
    let (server, adapter) = setup_github_mock().await;
    mock_create_pr(&server, 99, "https://github.com/owner/repo/pull/99").await;

    let result = adapter
        .create_pull_request(
            "owner",
            "repo",
            "feat/draft",
            "main",
            "Draft PR",
            Some("This is a draft"),
            true,
        )
        .await;

    assert!(
        result.is_ok(),
        "create draft PR should succeed: {:?}",
        result
    );
    let pr = result.unwrap();
    assert_eq!(pr.number, 99);
}

// ── PR Get ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_github_get_pr_open() {
    let (server, adapter) = setup_github_mock().await;
    mock_get_pr(&server, 42, "open", false).await;

    let result = adapter.get_pull_request("owner", "repo", 42).await;

    assert!(result.is_ok(), "get PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert_eq!(pr.number, 42);
    assert_eq!(pr.title, "Test PR");
    assert!(!pr.merged);
    assert_eq!(pr.head.ref_name, "feat/test");
    assert_eq!(pr.base.ref_name, "main");
    assert_eq!(pr.head.sha, "abc123def456");
}

#[tokio::test]
async fn test_github_get_pr_merged() {
    let (server, adapter) = setup_github_mock().await;
    mock_get_pr(&server, 42, "closed", true).await;

    let result = adapter.get_pull_request("owner", "repo", 42).await;

    assert!(result.is_ok());
    let pr = result.unwrap();
    assert!(pr.merged);
    assert_eq!(
        pr.state,
        gitgrip::platform::PRState::Merged,
        "merged PR should have Merged state"
    );
}

#[tokio::test]
async fn test_github_get_pr_not_found() {
    let (server, adapter) = setup_github_mock().await;
    mock_not_found(&server, "/repos/owner/repo/pulls/999").await;

    let result = adapter.get_pull_request("owner", "repo", 999).await;

    assert!(result.is_err(), "should fail for nonexistent PR");
    // Note: octocrab's error for 404 doesn't include "404" in the message,
    // so the adapter classifies it as ApiError rather than NotFound.
    // This is a known limitation of the error classification.
}

// ── PR Merge ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_github_merge_pr() {
    let (server, adapter) = setup_github_mock().await;
    mock_merge_pr(&server, 42, true).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, Some(MergeMethod::Squash), false)
        .await;

    assert!(result.is_ok(), "merge should succeed: {:?}", result);
    assert!(result.unwrap(), "PR should be merged");
}

#[tokio::test]
async fn test_github_merge_pr_not_mergeable() {
    let (server, adapter) = setup_github_mock().await;
    mock_merge_pr(&server, 42, false).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, None, false)
        .await;

    // The adapter returns Ok(false) when merge response says merged=false
    assert!(result.is_ok());
    assert!(!result.unwrap(), "PR should not be merged");
}

// ── PR Merge: Branch Behind ──────────────────────────────────────────

#[tokio::test]
async fn test_github_merge_pr_branch_behind() {
    let (server, adapter) = setup_github_mock().await;
    mock_merge_pr_behind(&server, 42).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, None, false)
        .await;

    assert!(result.is_err(), "should fail with BranchBehind");
    let err = result.unwrap_err();
    let err_str = err.to_string();
    assert!(
        err_str.contains("behind"),
        "error should mention 'behind': {}",
        err_str
    );
}

// ── PR Merge: Branch Protected ──────────────────────────────────────

#[tokio::test]
async fn test_github_merge_pr_branch_protected() {
    let (server, adapter) = setup_github_mock().await;
    mock_merge_pr_protected(&server, 42).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, None, false)
        .await;

    assert!(result.is_err(), "should fail with BranchProtected");
    let err = result.unwrap_err();
    let err_str = err.to_string();
    assert!(
        err_str.contains("protection") || err_str.contains("protected"),
        "error should mention protection: {}",
        err_str
    );
}

// ── Update Branch ──────────────────────────────────────────────────

#[tokio::test]
async fn test_github_update_branch_success() {
    let (server, adapter) = setup_github_mock().await;
    mock_update_branch(&server, 42).await;

    let result = adapter.update_branch("owner", "repo", 42).await;

    assert!(result.is_ok(), "update branch should succeed: {:?}", result);
    assert!(result.unwrap(), "should return true on successful update");
}

#[tokio::test]
async fn test_github_update_branch_conflict() {
    let (server, adapter) = setup_github_mock().await;
    mock_update_branch_conflict(&server, 42).await;

    let result = adapter.update_branch("owner", "repo", 42).await;

    assert!(result.is_err(), "should fail with conflict");
    let err_str = result.unwrap_err().to_string();
    assert!(
        err_str.contains("conflicts") || err_str.contains("conflict"),
        "error should mention conflicts: {}",
        err_str
    );
}

// ── Find PR by Branch ──────────────────────────────────────────────

#[tokio::test]
async fn test_github_find_pr_by_branch_found() {
    let (server, adapter) = setup_github_mock().await;
    mock_list_prs(&server, vec![(42, "feat/test")]).await;

    let result = adapter
        .find_pr_by_branch("owner", "repo", "feat/test")
        .await;

    assert!(result.is_ok(), "find PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert!(pr.is_some(), "should find PR for branch");
    let pr = pr.unwrap();
    assert_eq!(pr.number, 42);
}

#[tokio::test]
async fn test_github_find_pr_by_branch_not_found() {
    let (server, adapter) = setup_github_mock().await;
    mock_list_prs(&server, vec![]).await;

    let result = adapter
        .find_pr_by_branch("owner", "repo", "feat/nonexistent")
        .await;

    assert!(result.is_ok());
    assert!(result.unwrap().is_none(), "should not find PR");
}

// ── PR Reviews ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_github_pr_approved() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(&server, 42, vec![("APPROVED", "reviewer1")]).await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(result.unwrap(), "PR with approval should be approved");
}

#[tokio::test]
async fn test_github_pr_changes_requested() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(
        &server,
        42,
        vec![
            ("APPROVED", "reviewer1"),
            ("CHANGES_REQUESTED", "reviewer2"),
        ],
    )
    .await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(
        !result.unwrap(),
        "PR with changes requested should not be approved"
    );
}

#[tokio::test]
async fn test_github_pr_no_reviews() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(&server, 42, vec![]).await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(
        !result.unwrap(),
        "PR with no reviews should not be approved"
    );
}

#[tokio::test]
async fn test_github_pr_two_comment_reviews_accepted() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(
        &server,
        42,
        vec![("COMMENTED", "agent1"), ("COMMENTED", "agent2")],
    )
    .await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(
        result.unwrap(),
        "PR with 2+ comment reviews should be approved"
    );
}

#[tokio::test]
async fn test_github_pr_one_comment_review_not_enough() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(&server, 42, vec![("COMMENTED", "agent1")]).await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(
        !result.unwrap(),
        "PR with only 1 comment review should not be approved"
    );
}

#[tokio::test]
async fn test_github_pr_comment_reviews_blocked_by_changes_requested() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(
        &server,
        42,
        vec![
            ("COMMENTED", "agent1"),
            ("COMMENTED", "agent2"),
            ("CHANGES_REQUESTED", "agent3"),
        ],
    )
    .await;

    let result = adapter.is_pull_request_approved("owner", "repo", 42).await;

    assert!(result.is_ok());
    assert!(
        !result.unwrap(),
        "Comment reviews should not override changes requested"
    );
}

#[tokio::test]
async fn test_github_get_reviews() {
    let (server, adapter) = setup_github_mock().await;
    mock_pr_reviews(
        &server,
        42,
        vec![("APPROVED", "alice"), ("COMMENTED", "bob")],
    )
    .await;

    let result = adapter.get_pull_request_reviews("owner", "repo", 42).await;

    assert!(result.is_ok());
    let reviews = result.unwrap();
    assert_eq!(reviews.len(), 2);
    assert_eq!(reviews[0].user, "alice");
    assert_eq!(reviews[0].state, "Approved");
    assert_eq!(reviews[1].user, "bob");
}

// ── Status Checks ──────────────────────────────────────────────────

#[tokio::test]
async fn test_github_status_checks_all_pass() {
    let (server, adapter) = setup_github_mock().await;
    mock_check_runs(
        &server,
        "abc123",
        vec![
            ("CI", "completed", Some("success")),
            ("Lint", "completed", Some("success")),
        ],
    )
    .await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(result.is_ok(), "should get checks: {:?}", result);
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Success);
    assert_eq!(checks.statuses.len(), 2);
    assert!(
        checks.checks_configured,
        "real check runs must set checks_configured=true (grip#776 finding 2: \
         this was previously unasserted, so a mutant flipping the true case \
         to false went unnoticed)"
    );
}

#[tokio::test]
async fn test_github_status_checks_legacy_signal_trusted_despite_empty_check_runs() {
    // Check Runs API succeeds but reports zero runs (a repo whose CI posts
    // only to the legacy status API, never registering as a GitHub Actions
    // Check Run). The legacy endpoint DOES have a real signal -- it must be
    // trusted, not discarded just because Check Runs saw nothing.
    let (server, adapter) = setup_github_mock().await;
    mock_check_runs(&server, "abc123", vec![]).await;
    mock_legacy_combined_status(&server, "abc123", "success", vec![("legacy-ci", "success")]).await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(
        result.is_ok(),
        "should fall through to legacy: {:?}",
        result
    );
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Success);
    assert!(
        checks.checks_configured,
        "a real legacy signal must set checks_configured=true even when \
         Check Runs independently confirmed zero runs"
    );
}

#[tokio::test]
async fn test_github_status_checks_both_apis_confirm_nothing_configured() {
    // The base case grip#772 exists for: Check Runs positively confirms zero
    // runs AND the legacy endpoint is equally empty (pending + no statuses).
    // Both APIs agreeing on absence is the only case where checks_configured
    // should be false.
    let (server, adapter) = setup_github_mock().await;
    mock_check_runs(&server, "abc123", vec![]).await;
    mock_legacy_combined_status(&server, "abc123", "pending", vec![]).await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(
        result.is_ok(),
        "should fall through to legacy: {:?}",
        result
    );
    let checks = result.unwrap();
    assert!(
        !checks.checks_configured,
        "both APIs agreeing on zero checks must set checks_configured=false, \
         or `--wait` burns its full timeout on a ref with nothing to wait for \
         (grip#772)"
    );
}

#[tokio::test]
async fn test_github_status_checks_check_runs_500_with_ambiguous_legacy_is_not_confirmed_absent() {
    // grip#776 finding 1: Check Runs fails outright (500, not "succeeded
    // with zero"). The legacy endpoint's pending+empty reading is exactly as
    // ambiguous here as it is in the confirmed-empty case above -- but
    // reaching this fallback via a Check Runs FAILURE must not be conflated
    // with reaching it via a Check Runs CONFIRMATION of zero runs. An API
    // outage is not evidence of absence.
    let (server, adapter) = setup_github_mock().await;
    mock_server_error(&server, "/repos/owner/repo/commits/abc123/check-runs").await;
    mock_legacy_combined_status(&server, "abc123", "pending", vec![]).await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(
        result.is_ok(),
        "should fall through to legacy: {:?}",
        result
    );
    let checks = result.unwrap();
    assert!(
        checks.checks_configured,
        "Check Runs 500 + ambiguous legacy pending/empty must NOT be treated \
         as confirmed-absent -- that would let `--wait` silently proceed past \
         checks whose status could not actually be determined"
    );
}

#[tokio::test]
async fn test_github_status_checks_with_failure() {
    let (server, adapter) = setup_github_mock().await;
    mock_check_runs(
        &server,
        "abc123",
        vec![
            ("CI", "completed", Some("success")),
            ("Tests", "completed", Some("failure")),
        ],
    )
    .await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(result.is_ok());
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Failure);
}

#[tokio::test]
async fn test_github_status_checks_pending() {
    let (server, adapter) = setup_github_mock().await;
    mock_check_runs(
        &server,
        "abc123",
        vec![
            ("CI", "completed", Some("success")),
            ("Deploy", "in_progress", None),
        ],
    )
    .await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(result.is_ok());
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Pending);
}

// ── PR Diff ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_github_get_pr_diff() {
    let (server, adapter) = setup_github_mock().await;
    let diff =
        "diff --git a/file.rs b/file.rs\n--- a/file.rs\n+++ b/file.rs\n@@ -1 +1 @@\n-old\n+new\n";
    mock_pr_diff(&server, 42, diff).await;

    let result = adapter.get_pull_request_diff("owner", "repo", 42).await;

    assert!(result.is_ok(), "should get diff: {:?}", result);
    let got_diff = result.unwrap();
    assert!(got_diff.contains("+new"));
    assert!(got_diff.contains("-old"));
}

// ── Linked PR Comment Parsing ──────────────────────────────────────

#[tokio::test]
async fn test_github_linked_pr_in_body() {
    let (server, adapter) = setup_github_mock().await;
    mock_get_pr(&server, 42, "open", false).await;

    let result = adapter.get_pull_request("owner", "repo", 42).await;
    assert!(result.is_ok());

    let pr = result.unwrap();
    let links = adapter.parse_linked_pr_comment(&pr.body);
    assert_eq!(links.len(), 1);
    assert_eq!(links[0].repo_name, "frontend");
    assert_eq!(links[0].number, 42);
}

// ── PR Merge: Already Merged ─────────────────────────────────────

#[tokio::test]
async fn test_github_merge_pr_already_merged() {
    let (server, adapter) = setup_github_mock().await;
    // Merge response with merged=false indicates PR was not merged by this call
    // (e.g., already merged). The adapter returns Ok(false).
    mock_merge_pr(&server, 42, false).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, Some(MergeMethod::Merge), false)
        .await;

    assert!(result.is_ok(), "should not error: {:?}", result);
    assert!(
        !result.unwrap(),
        "merged should be false for already-merged PR"
    );
}

// ── PR Merge: 405 Generic Not Mergeable ─────────────────────────

#[tokio::test]
async fn test_github_merge_pr_405_generic() {
    let (server, adapter) = setup_github_mock().await;
    mock_merge_pr_405_generic(&server, 42).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, None, false)
        .await;

    // Generic 405 (not "branch behind") should return an error
    assert!(
        result.is_err(),
        "generic 405 should return Err: {:?}",
        result
    );
    let err_msg = result.unwrap_err().to_string();
    assert!(
        err_msg.contains("merge rejected"),
        "error should mention merge rejected: {}",
        err_msg
    );
}

// ── PR Create: Validation Error (422) ───────────────────────────

#[tokio::test]
async fn test_github_create_pr_validation_error() {
    let (server, adapter) = setup_github_mock().await;
    mock_create_pr_validation_error(&server).await;

    let result = adapter
        .create_pull_request("owner", "repo", "feat/test", "main", "Test PR", None, false)
        .await;

    assert!(result.is_err(), "should fail with validation error");
    let err_str = result.unwrap_err().to_string();
    assert!(
        err_str.contains("Failed to create PR"),
        "error should mention create failure: {}",
        err_str
    );
    assert!(
        err_str.contains("422"),
        "error should include upstream status code: {}",
        err_str
    );
    assert!(
        err_str.contains("A pull request already exists for owner:feat/test."),
        "error should include upstream validation detail: {}",
        err_str
    );
}

// ── Rate Limited (403) ──────────────────────────────────────────

#[tokio::test]
async fn test_github_rate_limited() {
    let (server, adapter) = setup_github_mock().await;
    mock_rate_limited(&server, "/repos/owner/repo/pulls/42").await;

    let result = adapter.get_pull_request("owner", "repo", 42).await;

    assert!(result.is_err(), "should fail when rate limited");
}

// ── Server Error (500) on Merge ─────────────────────────────────

#[tokio::test]
async fn test_github_server_error_on_merge() {
    let (server, adapter) = setup_github_mock().await;
    mock_server_error_put(&server, "/repos/owner/repo/pulls/42/merge").await;

    let result = adapter
        .merge_pull_request("owner", "repo", 42, None, false)
        .await;

    assert!(result.is_err(), "should fail on 500 server error");
    let err_str = result.unwrap_err().to_string();
    assert!(
        err_str.contains("500")
            || err_str.contains("Internal Server Error")
            || err_str.contains("Failed to merge"),
        "error should mention server error: {}",
        err_str
    );
}

// ── Error Scenarios ──────────────────────────────────────────────
// Note: test_github_auth_error_no_token lives in test_platform_github_auth.rs
// (separate binary) to avoid env var races with other tests in this file.

#[tokio::test]
async fn test_github_server_error_on_checks() {
    let (server, adapter) = setup_github_mock().await;
    mock_server_error(&server, "/repos/owner/repo/commits/abc123/check-runs").await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    // When check-runs returns 500, the adapter falls back to legacy status API
    // which also won't have a mock, so it should error
    assert!(result.is_err(), "should fail on server error");
}

// ── URL Parsing ──────────────────────────────────────────────────

#[test]
fn test_github_parse_ssh_url() {
    let adapter = gitgrip::platform::github::GitHubAdapter::new(None);
    let info = adapter
        .parse_repo_url("git@github.com:org/my-repo.git")
        .expect("should parse SSH URL");
    assert_eq!(info.owner, "org");
    assert_eq!(info.repo, "my-repo");
}

#[test]
fn test_github_parse_https_url() {
    let adapter = gitgrip::platform::github::GitHubAdapter::new(None);
    let info = adapter
        .parse_repo_url("https://github.com/org/my-repo.git")
        .expect("should parse HTTPS URL");
    assert_eq!(info.owner, "org");
    assert_eq!(info.repo, "my-repo");
}

#[test]
fn test_github_parse_invalid_url() {
    let adapter = gitgrip::platform::github::GitHubAdapter::new(None);
    let result = adapter.parse_repo_url("https://gitlab.com/org/repo.git");
    assert!(result.is_none(), "should not parse non-GitHub URL");
}

#[test]
fn test_github_matches_url() {
    let adapter = gitgrip::platform::github::GitHubAdapter::new(None);
    assert!(adapter.matches_url("git@github.com:user/repo.git"));
    assert!(adapter.matches_url("https://github.com/user/repo"));
    assert!(!adapter.matches_url("git@gitlab.com:user/repo.git"));
    assert!(!adapter.matches_url("https://dev.azure.com/org/proj/_git/repo"));
}
