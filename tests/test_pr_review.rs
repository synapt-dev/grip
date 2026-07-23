//! Command-orchestration regression for `gr pr review`'s multi-repo scope
//! guard (grip#770, grip#773).
//!
//! Sentinel's reviewer-2 finding on grip#773 (c650a85): the committed guard
//! tests all called `require_explicit_multi_repo_scope` directly, so nothing
//! in the suite would notice if the guard's call-site inside
//! `run_pr_review`'s own body were ever removed — the pure helper would stay
//! green while the real command silently regained the grip#770 cross-post
//! behavior. These tests exercise `run_pr_review` itself, end to end, and
//! assert on the wiremock server's received-request log: the guard must fire
//! BEFORE any review POST is sent, not just return the right Rust `Result`.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use common::mock_platform::{mock_list_prs, point_repo_at_mock, setup_github_mock};
use gitgrip::platform::ReviewEvent;
use wiremock::http::Method;

// `find_pr_by_branch`'s mock (`mock_list_prs`) doesn't filter by query string
// -- every repo's lookup resolves to the same first PR in the mocked list,
// which is sufficient to prove the "N repos matched" shape this guard cares
// about; it doesn't require each repo to resolve to a distinct real PR.

// ── grip#770 exact shape: three repos, unscoped review, zero POSTs ────────

#[tokio::test]
async fn test_pr_review_unscoped_multi_match_sends_zero_review_posts() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new()
        .add_repo("consult-conversa-config")
        .add_repo("premium")
        .add_repo("recall")
        .build();
    let mut manifest = ws.load_manifest();

    for repo_name in ["consult-conversa-config", "premium", "recall"] {
        git_helpers::create_branch(&ws.repo_path(repo_name), "sentinel/d5-gr1-bug-five");
        git_helpers::commit_file(
            &ws.repo_path(repo_name),
            "feature.txt",
            "feature",
            "Add feature",
        );
        point_repo_at_mock(&mut manifest, repo_name, &server);
    }

    mock_list_prs(&server, vec![(892, "sentinel/d5-gr1-bug-five")]).await;

    let result = gitgrip::cli::commands::pr::run_pr_review(
        &ws.workspace_root,
        &manifest,
        ReviewEvent::Comment,
        Some("a body meant for exactly one PR"),
        None,  // no --repo
        false, // no --all
        false,
    )
    .await;

    assert!(
        result.is_err(),
        "unscoped 3-repo match must be refused, not silently reviewed"
    );

    let requests = server.received_requests().await.unwrap();
    let review_posts: Vec<_> = requests
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/reviews"))
        .collect();
    assert!(
        review_posts.is_empty(),
        "expected zero review POSTs, got {}: {:?}",
        review_posts.len(),
        review_posts
            .iter()
            .map(|r| r.url.path())
            .collect::<Vec<_>>()
    );
}

// ── --repo explicitly scopes past the guard, review proceeds normally ─────

#[tokio::test]
async fn test_pr_review_explicit_repo_filter_proceeds_and_posts_review() {
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
    wiremock::Mock::given(wiremock::matchers::method("POST"))
        .and(wiremock::matchers::path(
            "/repos/owner/repo/pulls/1/reviews",
        ))
        .respond_with(
            wiremock::ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": 1, "state": "COMMENTED"
            })),
        )
        .mount(&server)
        .await;

    let result = gitgrip::cli::commands::pr::run_pr_review(
        &ws.workspace_root,
        &manifest,
        ReviewEvent::Comment,
        Some("scoped review body"),
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
    let review_posts = requests
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/reviews"))
        .count();
    assert_eq!(
        review_posts, 1,
        "expected exactly one review POST once explicitly scoped"
    );
}

// ── validate_repo_filters_known's command-level loud-fail (Opus, non-blocking) ──

#[tokio::test]
async fn test_pr_review_unknown_repo_filter_fails_loudly_not_silently() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let mut manifest = ws.load_manifest();
    git_helpers::create_branch(&ws.repo_path("app"), "feat/test");
    git_helpers::commit_file(&ws.repo_path("app"), "f.txt", "x", "Add f");
    point_repo_at_mock(&mut manifest, "app", &server);

    let result = gitgrip::cli::commands::pr::run_pr_review(
        &ws.workspace_root,
        &manifest,
        ReviewEvent::Comment,
        Some("body"),
        Some(&["this-repo-does-not-exist".to_string()]),
        false,
        false,
    )
    .await;

    let err = result.expect_err(
        "an unknown --repo filter value must fail loudly (validate_repo_filters_known), \
         not silently resolve to zero matched repos",
    );
    let message = err.to_string();
    assert!(
        message.contains("this-repo-does-not-exist"),
        "error should name the unknown filter, got: {message}"
    );
    assert!(
        message.contains("gr sync"),
        "error should carry the sync-guidance validate_repo_filters_known provides, got: {message}"
    );

    // No API calls should happen at all -- the filter validation runs before
    // any repo is even inspected for a matching PR.
    let requests = server.received_requests().await.unwrap();
    assert!(
        requests.is_empty(),
        "unknown-filter validation should fail before any API call, got {} requests",
        requests.len()
    );
}
