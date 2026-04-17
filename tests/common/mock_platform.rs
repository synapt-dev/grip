//! wiremock-based mock helpers for hosting platform API tests.
//!
//! Provides response builders for the GitHub API, allowing fully offline
//! testing of platform adapter methods.

use serde_json::{json, Map, Value};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

/// Start a wiremock server and configure GITHUB_TOKEN env var.
/// Returns the server and a GitHubAdapter pointed at it.
pub async fn setup_github_mock() -> (MockServer, gitgrip::platform::github::GitHubAdapter) {
    // Set token once before any tests use it. Using a static Once ensures
    // thread-safety even when tests run in parallel (set_var is not safe to
    // call concurrently on Rust 1.66+).
    use std::sync::Once;
    static SET_TOKEN: Once = Once::new();
    SET_TOKEN.call_once(|| unsafe {
        std::env::set_var("GITHUB_TOKEN", "mock-test-token");
    });

    let server = MockServer::start().await;
    let adapter = gitgrip::platform::github::GitHubAdapter::new(Some(&server.uri()));
    (server, adapter)
}

/// Generate a complete GitHub Author JSON object matching octocrab's Author struct.
fn github_user_json(login: &str, id: u64) -> Value {
    let api = format!("https://api.github.com/users/{}", login);
    let mut m = Map::new();
    m.insert("login".into(), json!(login));
    m.insert("id".into(), json!(id));
    m.insert("node_id".into(), json!(format!("MDQ6VXNlcjE{}", id)));
    m.insert(
        "avatar_url".into(),
        json!(format!(
            "https://avatars.githubusercontent.com/u/{}?v=4",
            id
        )),
    );
    m.insert("gravatar_id".into(), json!(""));
    m.insert("url".into(), json!(&api));
    m.insert(
        "html_url".into(),
        json!(format!("https://github.com/{}", login)),
    );
    m.insert("followers_url".into(), json!(format!("{}/followers", api)));
    m.insert(
        "following_url".into(),
        json!(format!("{}/following{{/other_user}}", api)),
    );
    m.insert(
        "gists_url".into(),
        json!(format!("{}/gists{{/gist_id}}", api)),
    );
    m.insert(
        "starred_url".into(),
        json!(format!("{}/starred{{/owner}}{{/repo}}", api)),
    );
    m.insert(
        "subscriptions_url".into(),
        json!(format!("{}/subscriptions", api)),
    );
    m.insert("organizations_url".into(), json!(format!("{}/orgs", api)));
    m.insert("repos_url".into(), json!(format!("{}/repos", api)));
    m.insert(
        "events_url".into(),
        json!(format!("{}/events{{/privacy}}", api)),
    );
    m.insert(
        "received_events_url".into(),
        json!(format!("{}/received_events", api)),
    );
    m.insert("type".into(), json!("User"));
    m.insert("site_admin".into(), json!(false));
    Value::Object(m)
}

/// Generate a complete GitHub repository JSON object that octocrab can deserialize.
/// Built programmatically to avoid macro recursion limits.
fn github_repo_json(owner: &str, repo: &str) -> Value {
    let base = format!("https://api.github.com/repos/{}/{}", owner, repo);
    let html = format!("https://github.com/{}/{}", owner, repo);

    let mut m = Map::new();
    m.insert("id".into(), json!(1));
    m.insert("node_id".into(), json!("MDEwOlJlcG9zaXRvcnkx"));
    m.insert("name".into(), json!(repo));
    m.insert("full_name".into(), json!(format!("{}/{}", owner, repo)));
    m.insert("private".into(), json!(false));
    m.insert("owner".into(), github_user_json(owner, 1));
    m.insert("html_url".into(), json!(html));
    m.insert("description".into(), Value::Null);
    m.insert("fork".into(), json!(false));
    m.insert("url".into(), json!(&base));

    // All the *_url fields octocrab expects
    let url_fields = [
        ("forks_url", "/forks"),
        ("keys_url", "/keys{/key_id}"),
        ("collaborators_url", "/collaborators{/collaborator}"),
        ("teams_url", "/teams"),
        ("hooks_url", "/hooks"),
        ("issue_events_url", "/issues/events{/number}"),
        ("events_url", "/events"),
        ("assignees_url", "/assignees{/user}"),
        ("branches_url", "/branches{/branch}"),
        ("tags_url", "/tags"),
        ("blobs_url", "/git/blobs{/sha}"),
        ("git_tags_url", "/git/tags{/sha}"),
        ("git_refs_url", "/git/refs{/sha}"),
        ("trees_url", "/git/trees{/sha}"),
        ("statuses_url", "/statuses/{sha}"),
        ("languages_url", "/languages"),
        ("stargazers_url", "/stargazers"),
        ("contributors_url", "/contributors"),
        ("subscribers_url", "/subscribers"),
        ("subscription_url", "/subscription"),
        ("commits_url", "/commits{/sha}"),
        ("git_commits_url", "/git/commits{/sha}"),
        ("comments_url", "/comments{/number}"),
        ("issue_comment_url", "/issues/comments{/number}"),
        ("contents_url", "/contents/{+path}"),
        ("compare_url", "/compare/{base}...{head}"),
        ("merges_url", "/merges"),
        ("archive_url", "/{archive_format}{/ref}"),
        ("downloads_url", "/downloads"),
        ("issues_url", "/issues{/number}"),
        ("pulls_url", "/pulls{/number}"),
        ("milestones_url", "/milestones{/number}"),
        (
            "notifications_url",
            "/notifications{?since,all,participating}",
        ),
        ("labels_url", "/labels{/name}"),
        ("releases_url", "/releases{/id}"),
        ("deployments_url", "/deployments"),
    ];

    for (field, suffix) in url_fields {
        m.insert(field.into(), json!(format!("{}{}", base, suffix)));
    }

    m.insert("created_at".into(), json!("2024-01-01T00:00:00Z"));
    m.insert("updated_at".into(), json!("2024-01-01T00:00:00Z"));
    m.insert("pushed_at".into(), json!("2024-01-01T00:00:00Z"));
    m.insert(
        "git_url".into(),
        json!(format!("git://github.com/{}/{}.git", owner, repo)),
    );
    m.insert(
        "ssh_url".into(),
        json!(format!("git@github.com:{}/{}.git", owner, repo)),
    );
    m.insert(
        "clone_url".into(),
        json!(format!("https://github.com/{}/{}.git", owner, repo)),
    );
    m.insert("svn_url".into(), json!(html));
    m.insert("homepage".into(), Value::Null);
    m.insert("size".into(), json!(0));
    m.insert("stargazers_count".into(), json!(0));
    m.insert("watchers_count".into(), json!(0));
    m.insert("language".into(), json!("Rust"));
    m.insert("has_issues".into(), json!(true));
    m.insert("has_projects".into(), json!(true));
    m.insert("has_downloads".into(), json!(true));
    m.insert("has_wiki".into(), json!(true));
    m.insert("has_pages".into(), json!(false));
    m.insert("forks_count".into(), json!(0));
    m.insert("mirror_url".into(), Value::Null);
    m.insert("archived".into(), json!(false));
    m.insert("disabled".into(), json!(false));
    m.insert("open_issues_count".into(), json!(0));
    m.insert("license".into(), Value::Null);
    m.insert("forks".into(), json!(0));
    m.insert("open_issues".into(), json!(0));
    m.insert("watchers".into(), json!(0));
    m.insert("default_branch".into(), json!("main"));
    m.insert("allow_squash_merge".into(), json!(true));
    m.insert("allow_merge_commit".into(), json!(true));
    m.insert("allow_rebase_merge".into(), json!(true));

    Value::Object(m)
}

/// Generate a complete GitHub PR JSON response that octocrab can deserialize.
fn github_pr_json(
    number: u64,
    state: &str,
    head_branch: &str,
    base_branch: &str,
    merged: bool,
    body: &str,
) -> Value {
    let repo = github_repo_json("owner", "repo");
    let api_base = "https://api.github.com/repos/owner/repo".to_string();

    let mut m = Map::new();
    m.insert("id".into(), json!(number));
    m.insert("number".into(), json!(number));
    m.insert("node_id".into(), json!(format!("PR_{}", number)));
    m.insert("state".into(), json!(state));
    m.insert("title".into(), json!("Test PR"));
    m.insert(
        "html_url".into(),
        json!(format!("https://github.com/owner/repo/pull/{}", number)),
    );
    m.insert(
        "diff_url".into(),
        json!(format!(
            "https://github.com/owner/repo/pull/{}.diff",
            number
        )),
    );
    m.insert(
        "patch_url".into(),
        json!(format!(
            "https://github.com/owner/repo/pull/{}.patch",
            number
        )),
    );
    m.insert(
        "issue_url".into(),
        json!(format!("{}/issues/{}", api_base, number)),
    );
    m.insert(
        "commits_url".into(),
        json!(format!("{}/pulls/{}/commits", api_base, number)),
    );
    m.insert(
        "review_comments_url".into(),
        json!(format!("{}/pulls/{}/comments", api_base, number)),
    );
    m.insert(
        "review_comment_url".into(),
        json!(format!("{}/pulls/comments{{/number}}", api_base)),
    );
    m.insert(
        "comments_url".into(),
        json!(format!("{}/issues/{}/comments", api_base, number)),
    );
    m.insert(
        "statuses_url".into(),
        json!(format!("{}/statuses/abc123def456", api_base)),
    );

    // Head
    m.insert(
        "head".into(),
        json!({
            "ref": head_branch,
            "sha": "abc123def456",
            "label": format!("owner:{}", head_branch),
            "repo": repo.clone(),
            "user": github_user_json("owner", 1)
        }),
    );

    // Base
    m.insert(
        "base".into(),
        json!({
            "ref": base_branch,
            "sha": "def456abc123",
            "label": format!("owner:{}", base_branch),
            "repo": repo,
            "user": github_user_json("owner", 1)
        }),
    );

    m.insert("body".into(), json!(body));
    m.insert("draft".into(), json!(false));
    m.insert("locked".into(), json!(false));
    m.insert("user".into(), github_user_json("testuser", 2));
    m.insert("merged".into(), json!(merged));
    m.insert(
        "merged_at".into(),
        if merged {
            json!("2024-01-02T00:00:00Z")
        } else {
            Value::Null
        },
    );
    m.insert("mergeable".into(), json!(!merged));
    m.insert("mergeable_state".into(), json!("clean"));
    m.insert(
        "merge_commit_sha".into(),
        if merged {
            json!("merge123")
        } else {
            Value::Null
        },
    );
    m.insert(
        "url".into(),
        json!(format!("{}/pulls/{}", api_base, number)),
    );
    m.insert("created_at".into(), json!("2024-01-01T00:00:00Z"));
    m.insert("updated_at".into(), json!("2024-01-01T00:00:00Z"));
    m.insert(
        "closed_at".into(),
        if state == "closed" || merged {
            json!("2024-01-02T00:00:00Z")
        } else {
            Value::Null
        },
    );
    m.insert("labels".into(), json!([]));
    m.insert("milestone".into(), Value::Null);
    m.insert("assignee".into(), Value::Null);
    m.insert("assignees".into(), json!([]));
    m.insert("requested_reviewers".into(), json!([]));
    m.insert("requested_teams".into(), json!([]));
    m.insert("active_lock_reason".into(), Value::Null);

    Value::Object(m)
}

/// GitHub API response for creating a PR (POST /repos/:owner/:repo/pulls).
pub async fn mock_create_pr(server: &MockServer, number: u64, html_url: &str) {
    let mut body = github_pr_json(number, "open", "feat/test", "main", false, "");
    body["html_url"] = json!(html_url);

    Mock::given(method("POST"))
        .and(path("/repos/owner/repo/pulls"))
        .respond_with(ResponseTemplate::new(201).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API response for getting a PR (GET /repos/:owner/:repo/pulls/:number).
pub async fn mock_get_pr(server: &MockServer, number: u64, state: &str, merged: bool) {
    let body = github_pr_json(
        number,
        state,
        "feat/test",
        "main",
        merged,
        "PR description\n<!-- gitgrip-linked-prs\nfrontend:42\n-->",
    );

    Mock::given(method("GET"))
        .and(path(format!("/repos/owner/repo/pulls/{}", number)))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API response for listing PRs (GET /repos/:owner/:repo/pulls).
pub async fn mock_list_prs(server: &MockServer, prs: Vec<(u64, &str)>) {
    let items: Vec<Value> = prs
        .iter()
        .map(|(number, branch)| github_pr_json(*number, "open", branch, "main", false, ""))
        .collect();

    Mock::given(method("GET"))
        .and(path("/repos/owner/repo/pulls"))
        .respond_with(ResponseTemplate::new(200).set_body_json(items))
        .mount(server)
        .await;
}

/// GitHub API response for merging a PR (PUT /repos/:owner/:repo/pulls/:number/merge).
pub async fn mock_merge_pr(server: &MockServer, number: u64, merged: bool) {
    let body = json!({
        "sha": "merge-sha-123",
        "merged": merged,
        "message": if merged { "Pull Request successfully merged" } else { "Pull Request is not mergeable" }
    });

    Mock::given(method("PUT"))
        .and(path(format!("/repos/owner/repo/pulls/{}/merge", number)))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: merge PR returns 405 with "branch behind" message.
pub async fn mock_merge_pr_behind(server: &MockServer, number: u64) {
    let body = json!({
        "message": "Head branch was behind base branch",
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("PUT"))
        .and(path(format!("/repos/owner/repo/pulls/{}/merge", number)))
        .respond_with(ResponseTemplate::new(405).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: merge PR returns 403 with "protected branch" message.
pub async fn mock_merge_pr_protected(server: &MockServer, number: u64) {
    let body = json!({
        "message": "At least 1 approving review is required by reviewers with write access. Protected branch rules not satisfied.",
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("PUT"))
        .and(path(format!("/repos/owner/repo/pulls/{}/merge", number)))
        .respond_with(ResponseTemplate::new(403).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: update branch success (PUT /repos/:owner/:repo/pulls/:number/update-branch).
pub async fn mock_update_branch(server: &MockServer, number: u64) {
    let body = json!({
        "message": "Updating pull request branch.",
        "url": format!("https://github.com/owner/repo/pull/{}", number)
    });

    Mock::given(method("PUT"))
        .and(path(format!(
            "/repos/owner/repo/pulls/{}/update-branch",
            number
        )))
        .respond_with(ResponseTemplate::new(202).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: update branch conflict (PUT /repos/:owner/:repo/pulls/:number/update-branch).
pub async fn mock_update_branch_conflict(server: &MockServer, number: u64) {
    let body = json!({
        "message": "merge conflict between base and head",
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("PUT"))
        .and(path(format!(
            "/repos/owner/repo/pulls/{}/update-branch",
            number
        )))
        .respond_with(ResponseTemplate::new(422).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API response for PR reviews (GET /repos/:owner/:repo/pulls/:number/reviews).
pub async fn mock_pr_reviews(server: &MockServer, number: u64, reviews: Vec<(&str, &str)>) {
    let items: Vec<Value> = reviews
        .iter()
        .enumerate()
        .map(|(i, (state, user))| {
            json!({
                "id": i + 1,
                "node_id": format!("PRR_{}", i + 1),
                "state": state,
                "user": github_user_json(user, (i + 1) as u64),
                "body": "",
                "html_url": format!("https://github.com/owner/repo/pull/{}", number),
                "pull_request_url": format!("https://api.github.com/repos/owner/repo/pulls/{}", number),
                "submitted_at": "2024-01-01T00:00:00Z",
                "commit_id": "abc123def456",
                "_links": {
                    "html": { "href": format!("https://github.com/owner/repo/pull/{}", number) },
                    "pull_request": { "href": format!("https://api.github.com/repos/owner/repo/pulls/{}", number) }
                }
            })
        })
        .collect();

    Mock::given(method("GET"))
        .and(path(format!("/repos/owner/repo/pulls/{}/reviews", number)))
        .respond_with(ResponseTemplate::new(200).set_body_json(items))
        .mount(server)
        .await;
}

/// GitHub API response for check runs (GET /repos/:owner/:repo/commits/:ref/check-runs).
pub async fn mock_check_runs(
    server: &MockServer,
    ref_name: &str,
    checks: Vec<(&str, &str, Option<&str>)>,
) {
    let check_runs: Vec<Value> = checks
        .iter()
        .enumerate()
        .map(|(i, (name, status, conclusion))| {
            let mut run = json!({
                "id": i + 1,
                "name": name,
                "status": status,
                "head_sha": "abc123"
            });
            if let Some(c) = conclusion {
                run["conclusion"] = json!(c);
            }
            run
        })
        .collect();

    let body = json!({
        "total_count": check_runs.len(),
        "check_runs": check_runs
    });

    Mock::given(method("GET"))
        .and(path(format!(
            "/repos/owner/repo/commits/{}/check-runs",
            ref_name
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API response for PR diff (GET /repos/:owner/:repo/pulls/:number with Accept: diff).
pub async fn mock_pr_diff(server: &MockServer, number: u64, diff_content: &str) {
    Mock::given(method("GET"))
        .and(path(format!("/repos/owner/repo/pulls/{}", number)))
        .and(header("Accept", "application/vnd.github.v3.diff"))
        .respond_with(ResponseTemplate::new(200).set_body_string(diff_content))
        .mount(server)
        .await;
}

/// Mock a 404 response for any GET to a specific path.
pub async fn mock_not_found(server: &MockServer, path_str: &str) {
    let body = json!({
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("GET"))
        .and(path(path_str))
        .respond_with(ResponseTemplate::new(404).set_body_json(body))
        .mount(server)
        .await;
}

/// Mock a server error (500).
pub async fn mock_server_error(server: &MockServer, path_str: &str) {
    let body = json!({
        "message": "Internal Server Error"
    });

    Mock::given(method("GET"))
        .and(path(path_str))
        .respond_with(ResponseTemplate::new(500).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: merge PR returns 405 with a generic "not mergeable" message.
pub async fn mock_merge_pr_405_generic(server: &MockServer, number: u64) {
    let body = json!({
        "message": "Pull Request is not mergeable",
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("PUT"))
        .and(path(format!("/repos/owner/repo/pulls/{}/merge", number)))
        .respond_with(ResponseTemplate::new(405).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: create PR returns 422 validation error.
pub async fn mock_create_pr_validation_error(server: &MockServer) {
    let body = json!({
        "message": "Validation Failed",
        "errors": [{"resource": "PullRequest", "code": "custom", "message": "A pull request already exists for owner:feat/test."}],
        "documentation_url": "https://docs.github.com/rest"
    });

    Mock::given(method("POST"))
        .and(path("/repos/owner/repo/pulls"))
        .respond_with(ResponseTemplate::new(422).set_body_json(body))
        .mount(server)
        .await;
}

/// GitHub API: 403 rate limited response on a GET endpoint.
pub async fn mock_rate_limited(server: &MockServer, path_str: &str) {
    let body = json!({
        "message": "API rate limit exceeded for user.",
        "documentation_url": "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting"
    });

    Mock::given(method("GET"))
        .and(path(path_str))
        .respond_with(
            ResponseTemplate::new(403)
                .set_body_json(body)
                .insert_header("X-RateLimit-Remaining", "0")
                .insert_header("X-RateLimit-Reset", "1700000000"),
        )
        .mount(server)
        .await;
}

/// Mock a PUT server error (500) — useful for merge endpoint.
pub async fn mock_server_error_put(server: &MockServer, path_str: &str) {
    let body = json!({
        "message": "Internal Server Error"
    });

    Mock::given(method("PUT"))
        .and(path(path_str))
        .respond_with(ResponseTemplate::new(500).set_body_json(body))
        .mount(server)
        .await;
}

/// Mock a GitHub repo info response (GET /repos/:owner/:repo).
pub async fn mock_repo_info(server: &MockServer, owner: &str, repo: &str) {
    let body = github_repo_json(owner, repo);

    Mock::given(method("GET"))
        .and(path(format!("/repos/{}/{}", owner, repo)))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

// ── Bitbucket mock helpers ──────────────────────────────────────────────────

/// Start a wiremock server and configure BITBUCKET_TOKEN env var.
/// Returns the server and a BitbucketAdapter pointed at it.
pub async fn setup_bitbucket_mock() -> (MockServer, gitgrip::platform::bitbucket::BitbucketAdapter)
{
    use std::sync::Once;
    static SET_TOKEN: Once = Once::new();
    SET_TOKEN.call_once(|| unsafe {
        std::env::set_var("BITBUCKET_TOKEN", "mock-bb-token");
    });

    let server = MockServer::start().await;
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(Some(&server.uri()));
    (server, adapter)
}

/// Generate a Bitbucket PR JSON response.
fn bb_pr_json(id: u64, state: &str, head_branch: &str, base_branch: &str) -> Value {
    json!({
        "id": id,
        "title": "Test PR",
        "description": "PR description",
        "state": state,
        "source": { "branch": { "name": head_branch } },
        "destination": { "branch": { "name": base_branch } },
        "links": {
            "html": { "href": format!("https://bitbucket.org/owner/repo/pull-requests/{}", id) }
        }
    })
}

/// Bitbucket API: create PR (POST /repositories/:owner/:repo/pullrequests).
pub async fn mock_bb_create_pr(server: &MockServer, id: u64) {
    let body = bb_pr_json(id, "OPEN", "feat/test", "main");

    Mock::given(method("POST"))
        .and(path("/repositories/owner/repo/pullrequests"))
        .respond_with(ResponseTemplate::new(201).set_body_json(body))
        .mount(server)
        .await;
}

/// Bitbucket API: get PR (GET /repositories/:owner/:repo/pullrequests/:id).
pub async fn mock_bb_get_pr(server: &MockServer, id: u64, state: &str) {
    let body = bb_pr_json(id, state, "feat/test", "main");

    Mock::given(method("GET"))
        .and(path(format!(
            "/repositories/owner/repo/pullrequests/{}",
            id
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// Bitbucket API: merge PR (POST /repositories/:owner/:repo/pullrequests/:id/merge).
pub async fn mock_bb_merge_pr(server: &MockServer, id: u64) {
    let body = bb_pr_json(id, "MERGED", "feat/test", "main");

    Mock::given(method("POST"))
        .and(path(format!(
            "/repositories/owner/repo/pullrequests/{}/merge",
            id
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// Bitbucket API: find PR by branch (GET /repositories/:owner/:repo/pullrequests?...).
pub async fn mock_bb_find_pr(server: &MockServer, prs: Vec<(u64, &str)>) {
    let values: Vec<Value> = prs
        .iter()
        .map(|(id, branch)| bb_pr_json(*id, "OPEN", branch, "main"))
        .collect();

    let body = json!({ "values": values });

    Mock::given(method("GET"))
        .and(path("/repositories/owner/repo/pullrequests"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// Bitbucket API: commit statuses (GET /repositories/:owner/:repo/commits/:ref/statuses).
pub async fn mock_bb_status_checks(
    server: &MockServer,
    ref_name: &str,
    statuses: Vec<(&str, &str)>,
) {
    let values: Vec<Value> = statuses
        .iter()
        .map(|(key, state)| {
            json!({
                "key": key,
                "state": state
            })
        })
        .collect();

    let body = json!({ "values": values });

    Mock::given(method("GET"))
        .and(path(format!(
            "/repositories/owner/repo/commits/{}/statuses",
            ref_name
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}

/// Bitbucket API: reviewers (GET /repositories/:owner/:repo/pullrequests/:id/default-reviewers).
pub async fn mock_bb_reviewers(server: &MockServer, id: u64, reviewers: Vec<bool>) {
    let values: Vec<Value> = reviewers
        .iter()
        .map(|approved| json!({ "approved": approved }))
        .collect();

    let body = json!({ "values": values });

    Mock::given(method("GET"))
        .and(path(format!(
            "/repositories/owner/repo/pullrequests/{}/default-reviewers",
            id
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(server)
        .await;
}
