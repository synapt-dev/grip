//! Integration tests for the Bitbucket platform adapter using wiremock.
//!
//! Tests the BitbucketAdapter against mock HTTP responses, verifying correct
//! API interaction without requiring real Bitbucket credentials or network access.

mod common;

use common::mock_platform::*;
use gitgrip::platform::traits::HostingPlatform;
use gitgrip::platform::CheckState;

// ── PR Create ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_bb_create_pr() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_create_pr(&server, 10).await;

    let result = adapter
        .create_pull_request("owner", "repo", "feat/test", "main", "Test PR", None, false)
        .await;

    assert!(result.is_ok(), "create PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert_eq!(pr.number, 10);
    assert!(pr.url.contains("bitbucket.org"));
}

// ── PR Get ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_bb_get_pr_open() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_get_pr(&server, 10, "OPEN").await;

    let result = adapter.get_pull_request("owner", "repo", 10).await;

    assert!(result.is_ok(), "get PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert_eq!(pr.number, 10);
    assert!(!pr.merged);
    assert_eq!(pr.state, gitgrip::platform::PRState::Open);
    assert_eq!(pr.head.ref_name, "feat/test");
    assert_eq!(pr.base.ref_name, "main");
}

#[tokio::test]
async fn test_bb_get_pr_merged() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_get_pr(&server, 10, "MERGED").await;

    let result = adapter.get_pull_request("owner", "repo", 10).await;

    assert!(result.is_ok());
    let pr = result.unwrap();
    assert!(pr.merged);
    assert_eq!(pr.state, gitgrip::platform::PRState::Merged);
}

// ── PR Merge ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_bb_merge_pr() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_merge_pr(&server, 10).await;

    let result = adapter
        .merge_pull_request("owner", "repo", 10, None, false)
        .await;

    assert!(result.is_ok(), "merge should succeed: {:?}", result);
    assert!(result.unwrap(), "PR should be merged");
}

// ── Find PR by Branch ──────────────────────────────────────────────

#[tokio::test]
async fn test_bb_find_pr_by_branch() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_find_pr(&server, vec![(10, "feat/test")]).await;

    let result = adapter
        .find_pr_by_branch("owner", "repo", "feat/test")
        .await;

    assert!(result.is_ok(), "find PR should succeed: {:?}", result);
    let pr = result.unwrap();
    assert!(pr.is_some(), "should find PR for branch");
    assert_eq!(pr.unwrap().number, 10);
}

#[tokio::test]
async fn test_bb_find_pr_not_found() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_find_pr(&server, vec![]).await;

    let result = adapter
        .find_pr_by_branch("owner", "repo", "feat/nonexistent")
        .await;

    assert!(result.is_ok());
    assert!(result.unwrap().is_none(), "should not find PR");
}

// ── Status Checks ──────────────────────────────────────────────────

#[tokio::test]
async fn test_bb_status_checks() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_status_checks(
        &server,
        "abc123",
        vec![("CI", "SUCCESSFUL"), ("Lint", "SUCCESSFUL")],
    )
    .await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(result.is_ok(), "should get checks: {:?}", result);
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Success);
    assert_eq!(checks.statuses.len(), 2);
    assert!(
        checks.checks_configured,
        "real posted statuses must set checks_configured=true (grip#776 \
         finding 2: this was previously unasserted, so a mutant flipping the \
         true case to false went unnoticed)"
    );
}

#[tokio::test]
async fn test_bb_status_checks_empty_sets_checks_configured_false() {
    // grip#776 finding 2 (Bitbucket's empty side): zero posted statuses
    // means nothing is configured for this ref, not "checks are running" --
    // the same false-absence risk grip#772 fixed for GitHub. Never
    // independently pinned for the Bitbucket adapter until now.
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_status_checks(&server, "abc123", vec![]).await;

    let result = adapter.get_status_checks("owner", "repo", "abc123").await;

    assert!(result.is_ok(), "should get checks: {:?}", result);
    let checks = result.unwrap();
    assert_eq!(checks.state, CheckState::Pending);
    assert!(
        !checks.checks_configured,
        "zero posted statuses must set checks_configured=false, or `--wait` \
         burns its full timeout on a ref with nothing to wait for"
    );
}

// ── Approval ──────────────────────────────────────────────────────

#[tokio::test]
async fn test_bb_is_approved() {
    let (server, adapter) = setup_bitbucket_mock().await;
    mock_bb_reviewers(&server, 10, vec![true, true]).await;

    let result = adapter.is_pull_request_approved("owner", "repo", 10).await;

    assert!(result.is_ok());
    assert!(result.unwrap(), "PR with all approvals should be approved");
}

// ── URL Parsing ──────────────────────────────────────────────────

#[test]
fn test_bb_parse_repo_url_ssh() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);
    let info = adapter
        .parse_repo_url("git@bitbucket.org:myteam/my-repo.git")
        .expect("should parse SSH URL");
    assert_eq!(info.owner, "myteam");
    assert_eq!(info.repo, "my-repo");
}

#[test]
fn test_bb_parse_repo_url_https() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);
    let info = adapter
        .parse_repo_url("https://bitbucket.org/myteam/my-repo.git")
        .expect("should parse HTTPS URL");
    assert_eq!(info.owner, "myteam");
    assert_eq!(info.repo, "my-repo");
}

// ── Linked PR Comment ──────────────────────────────────────────────

#[test]
fn test_bb_linked_pr_comment() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);

    let links = vec![
        gitgrip::platform::traits::LinkedPRRef {
            repo_name: "frontend".to_string(),
            number: 42,
        },
        gitgrip::platform::traits::LinkedPRRef {
            repo_name: "backend".to_string(),
            number: 99,
        },
    ];

    let comment = adapter.generate_linked_pr_comment(&links);
    let parsed = adapter.parse_linked_pr_comment(&comment);

    assert_eq!(parsed.len(), 2);
    assert_eq!(parsed[0].repo_name, "frontend");
    assert_eq!(parsed[0].number, 42);
    assert_eq!(parsed[1].repo_name, "backend");
    assert_eq!(parsed[1].number, 99);
}
