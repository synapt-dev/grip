//! GitHub platform adapter

use async_trait::async_trait;
use octocrab::Octocrab;
use std::env;
use std::time::Duration;

use super::http::{check_response_rate_limit, create_http_client};
use super::traits::{HostingPlatform, PlatformError};
use super::types::*;
use crate::core::manifest::PlatformType;

/// Default connection timeout in seconds
const CONNECT_TIMEOUT_SECS: u64 = 10;
/// Default read timeout in seconds
const READ_TIMEOUT_SECS: u64 = 30;
/// Default write timeout in seconds
const WRITE_TIMEOUT_SECS: u64 = 30;

#[cfg(feature = "telemetry")]
use crate::telemetry::metrics::GLOBAL_METRICS;
#[cfg(feature = "telemetry")]
use std::time::Instant;
#[cfg(feature = "telemetry")]
use tracing::debug;

#[cfg(not(feature = "telemetry"))]
use tracing::debug;

/// GitHub API adapter
pub struct GitHubAdapter {
    base_url: Option<String>,
}

impl GitHubAdapter {
    /// Create a new GitHub adapter
    pub fn new(base_url: Option<&str>) -> Self {
        Self {
            base_url: base_url.map(|s| s.to_string()),
        }
    }

    /// Create a configured HTTP client with timeouts
    fn http_client() -> reqwest::Client {
        create_http_client()
    }

    /// Get configured Octocrab instance with proper timeouts
    async fn get_client(&self) -> Result<Octocrab, PlatformError> {
        let token = self.get_token().await?;

        let mut builder = Octocrab::builder()
            .personal_token(token)
            .set_connect_timeout(Some(Duration::from_secs(CONNECT_TIMEOUT_SECS)))
            .set_read_timeout(Some(Duration::from_secs(READ_TIMEOUT_SECS)))
            .set_write_timeout(Some(Duration::from_secs(WRITE_TIMEOUT_SECS)));

        if let Some(ref base_url) = self.base_url {
            builder = builder
                .base_uri(base_url)
                .map_err(|e| PlatformError::ApiError(format!("Invalid base URL: {}", e)))?;
        }

        builder
            .build()
            .map_err(|e| PlatformError::ApiError(format!("Failed to create client: {}", e)))
    }
}

/// Shared deserialization structs for GitHub issue responses.
/// Used by both `list_issues` and `get_issue`.
#[derive(serde::Deserialize)]
struct GhIssueDetail {
    number: u64,
    html_url: String,
    title: String,
    body: Option<String>,
    state: String,
    labels: Vec<GhIssueLabel>,
    assignees: Vec<GhIssueUser>,
    user: Option<GhIssueUser>,
    created_at: String,
    updated_at: String,
    pull_request: Option<serde_json::Value>,
}

#[derive(serde::Deserialize)]
struct GhIssueLabel {
    name: String,
    color: Option<String>,
}

#[derive(serde::Deserialize)]
struct GhIssueUser {
    login: String,
}

impl GhIssueDetail {
    fn into_issue(self) -> Issue {
        Issue {
            number: self.number,
            url: self.html_url,
            title: self.title,
            body: self.body.unwrap_or_default(),
            state: if self.state == "open" {
                IssueState::Open
            } else {
                IssueState::Closed
            },
            labels: self
                .labels
                .into_iter()
                .map(|l| IssueLabel {
                    name: l.name,
                    color: l.color,
                })
                .collect(),
            assignees: self.assignees.into_iter().map(|a| a.login).collect(),
            author: self.user.map(|u| u.login).unwrap_or_default(),
            created_at: self.created_at,
            updated_at: self.updated_at,
        }
    }
}

#[async_trait]
impl HostingPlatform for GitHubAdapter {
    fn platform_type(&self) -> PlatformType {
        PlatformType::GitHub
    }

    async fn get_token(&self) -> Result<String, PlatformError> {
        // Try environment variables first
        if let Ok(token) = env::var("GITHUB_TOKEN") {
            return Ok(token);
        }
        if let Ok(token) = env::var("GH_TOKEN") {
            return Ok(token);
        }

        // Try gh CLI auth
        debug!(target: "gitgrip::cmd", program = "gh", args = ?["auth", "token"], "exec");
        let output = tokio::process::Command::new("gh")
            .args(["auth", "token"])
            .output()
            .await
            .map_err(|e| PlatformError::AuthError(format!("Failed to run gh auth: {}", e)))?;

        if output.status.success() {
            let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !token.is_empty() {
                return Ok(token);
            }
        }

        Err(PlatformError::AuthError(
            "No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'".to_string(),
        ))
    }

    async fn create_pull_request(
        &self,
        owner: &str,
        repo: &str,
        head: &str,
        base: &str,
        title: &str,
        body: Option<&str>,
        draft: bool,
    ) -> Result<PRCreateResult, PlatformError> {
        #[cfg(feature = "telemetry")]
        let start = Instant::now();

        let client = self.get_client().await?;

        let result = client
            .pulls(owner, repo)
            .create(title, head, base)
            .body(body.unwrap_or(""))
            .draft(draft)
            .send()
            .await;

        #[cfg(feature = "telemetry")]
        {
            let duration = start.elapsed();
            let success = result.is_ok();
            GLOBAL_METRICS.record_platform("github", "create_pr", duration, success);
            debug!(
                owner,
                repo,
                head,
                base,
                draft,
                success,
                duration_ms = duration.as_millis() as u64,
                "GitHub create PR complete"
            );
        }

        let pr =
            result.map_err(|e| PlatformError::ApiError(format!("Failed to create PR: {}", e)))?;

        Ok(PRCreateResult {
            number: pr.number,
            url: pr.html_url.map(|u| u.to_string()).unwrap_or_default(),
        })
    }

    async fn get_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<PullRequest, PlatformError> {
        let client = self.get_client().await?;

        let pr = client
            .pulls(owner, repo)
            .get(pull_number)
            .await
            .map_err(|e| {
                if e.to_string().contains("404") {
                    PlatformError::NotFound(format!("PR #{} not found", pull_number))
                } else {
                    PlatformError::ApiError(format!("Failed to get PR: {}", e))
                }
            })?;

        let state = if pr.merged_at.is_some() {
            PRState::Merged
        } else {
            match pr.state {
                Some(octocrab::models::IssueState::Open) => PRState::Open,
                Some(octocrab::models::IssueState::Closed) => PRState::Closed,
                _ => PRState::Open,
            }
        };

        Ok(PullRequest {
            number: pr.number,
            url: pr.html_url.map(|u| u.to_string()).unwrap_or_default(),
            title: pr.title.clone().unwrap_or_default(),
            body: pr.body.clone().unwrap_or_default(),
            state,
            merged: pr.merged_at.is_some(),
            mergeable: pr.mergeable,
            head: PRHead {
                ref_name: pr.head.ref_field.clone(),
                sha: pr.head.sha.clone(),
            },
            base: PRBase {
                ref_name: pr.base.ref_field.clone(),
            },
        })
    }

    async fn update_pull_request_body(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        body: &str,
    ) -> Result<(), PlatformError> {
        let client = self.get_client().await?;

        client
            .pulls(owner, repo)
            .update(pull_number)
            .body(body)
            .send()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to update PR body: {}", e)))?;

        Ok(())
    }

    async fn update_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        title: Option<&str>,
        body: Option<&str>,
    ) -> Result<(), PlatformError> {
        let client = self.get_client().await?;
        let pulls = client.pulls(owner, repo);
        let mut builder = pulls.update(pull_number);

        if let Some(t) = title {
            builder = builder.title(t);
        }
        if let Some(b) = body {
            builder = builder.body(b);
        }

        builder
            .send()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to update PR: {}", e)))?;

        Ok(())
    }

    async fn merge_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        method: Option<MergeMethod>,
        _delete_branch: bool,
    ) -> Result<bool, PlatformError> {
        #[cfg(feature = "telemetry")]
        let start = Instant::now();

        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let merge_method_str = match method.unwrap_or(MergeMethod::Merge) {
            MergeMethod::Merge => "merge",
            MergeMethod::Squash => "squash",
            MergeMethod::Rebase => "rebase",
        };

        let url = format!(
            "{}/repos/{}/{}/pulls/{}/merge",
            base_url, owner, repo, pull_number
        );

        let http_client = Self::http_client();
        let response = http_client
            .put(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&serde_json::json!({ "merge_method": merge_method_str }))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        let status = response.status().as_u16();
        let body_text = response.text().await.unwrap_or_default();

        #[cfg(feature = "telemetry")]
        {
            let duration = start.elapsed();
            let success = status == 200;
            GLOBAL_METRICS.record_platform("github", "merge_pr", duration, success);
            debug!(
                owner,
                repo,
                pull_number,
                success,
                duration_ms = duration.as_millis() as u64,
                "GitHub merge PR complete"
            );
        }

        let body_lower = body_text.to_lowercase();

        match status {
            200 => {
                // Parse merged field from response
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body_text) {
                    Ok(parsed["merged"].as_bool().unwrap_or(false))
                } else {
                    Ok(true) // 200 status means success
                }
            }
            405 => {
                if body_lower.contains("head branch was behind")
                    || body_lower.contains("not up to date")
                {
                    Err(PlatformError::BranchBehind(format!(
                        "PR #{} branch is behind base branch",
                        pull_number
                    )))
                } else {
                    // Other 405 errors (not mergeable, checks required, etc.)
                    Err(PlatformError::ApiError(format!(
                        "PR #{} merge rejected (405): {}",
                        pull_number, body_text
                    )))
                }
            }
            403 => {
                if body_lower.contains("protected branch") || body_lower.contains("required") {
                    Err(PlatformError::BranchProtected(format!(
                        "PR #{} is blocked by branch protection rules",
                        pull_number
                    )))
                } else {
                    Err(PlatformError::ApiError(format!(
                        "Failed to merge PR (403): {}",
                        body_text
                    )))
                }
            }
            _ => Err(PlatformError::ApiError(format!(
                "Failed to merge PR ({}): {}",
                status, body_text
            ))),
        }
    }

    async fn update_branch(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<bool, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!(
            "{}/repos/{}/{}/pulls/{}/update-branch",
            base_url, owner, repo, pull_number
        );

        let http_client = Self::http_client();
        let response = http_client
            .put(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&serde_json::json!({}))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        match response.status().as_u16() {
            202 => Ok(true),
            422 => Err(PlatformError::ApiError(
                "Cannot update branch: conflicts exist that must be resolved manually".to_string(),
            )),
            status => {
                let error_text = response.text().await.unwrap_or_default();
                Err(PlatformError::ApiError(format!(
                    "Failed to update branch ({}): {}",
                    status, error_text
                )))
            }
        }
    }

    /// Enable auto-merge via `gh` CLI. Uses the CLI instead of the GraphQL API
    /// because the REST API doesn't support auto-merge and the GraphQL mutation
    /// is complex.
    ///
    /// Supports GitHub Enterprise by passing `--hostname` when a custom
    /// `base_url` is configured.
    async fn enable_auto_merge(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        method: Option<MergeMethod>,
    ) -> Result<bool, PlatformError> {
        let merge_flag = match method.unwrap_or(MergeMethod::Squash) {
            MergeMethod::Merge => "--merge",
            MergeMethod::Squash => "--squash",
            MergeMethod::Rebase => "--rebase",
        };

        let repo_arg = format!("{}/{}", owner, repo);
        let pr_str = pull_number.to_string();

        // Extract hostname for GHE instances
        let ghe_hostname = self.base_url.as_ref().and_then(|url| {
            url::Url::parse(url).ok().and_then(|parsed| {
                let host = parsed.host_str()?.to_string();
                if host == "api.github.com" {
                    None // Standard GitHub, no --hostname needed
                } else {
                    Some(host)
                }
            })
        });

        let mut args = vec![
            "pr", "merge", &pr_str, "--auto", merge_flag, "--repo", &repo_arg,
        ];
        if let Some(ref hostname) = ghe_hostname {
            args.push("--hostname");
            args.push(hostname);
        }

        let mut cmd = tokio::process::Command::new("gh");
        cmd.args(&args);

        debug!(target: "gitgrip::cmd", program = "gh", args = ?args, "exec");
        let output = cmd
            .output()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to run gh CLI: {}", e)))?;

        if output.status.success() {
            Ok(true)
        } else {
            let stderr = String::from_utf8_lossy(&output.stderr);
            Err(PlatformError::ApiError(format!(
                "Failed to enable auto-merge for PR #{}: {}",
                pull_number,
                stderr.trim()
            )))
        }
    }

    async fn find_pr_by_branch(
        &self,
        owner: &str,
        repo: &str,
        branch: &str,
    ) -> Result<Option<PRCreateResult>, PlatformError> {
        let client = self.get_client().await?;

        let prs = client
            .pulls(owner, repo)
            .list()
            .state(octocrab::params::State::Open)
            .head(format!("{}:{}", owner, branch))
            .send()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to find PR: {}", e)))?;

        if let Some(pr) = prs.items.first() {
            Ok(Some(PRCreateResult {
                number: pr.number,
                url: pr
                    .html_url
                    .as_ref()
                    .map(|u| u.to_string())
                    .unwrap_or_default(),
            }))
        } else {
            Ok(None)
        }
    }

    async fn list_pull_requests(
        &self,
        owner: &str,
        repo: &str,
        filter: &PRListFilter,
    ) -> Result<Vec<PullRequest>, PlatformError> {
        let client = self.get_client().await?;

        let state = match filter.state {
            Some(PRState::Open) => octocrab::params::State::Open,
            Some(PRState::Closed) | Some(PRState::Merged) => octocrab::params::State::Closed,
            None => octocrab::params::State::All,
        };

        // GitHub API max per_page is 100; clamp to u8 for octocrab
        let limit = filter.limit.unwrap_or(30).min(100) as u8;

        let prs = client
            .pulls(owner, repo)
            .list()
            .state(state)
            .per_page(limit)
            .send()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to list PRs: {}", e)))?;

        let mut result: Vec<PullRequest> = Vec::new();
        for pr in &prs.items {
            let pr_state = if pr.merged_at.is_some() {
                PRState::Merged
            } else {
                match pr.state {
                    Some(octocrab::models::IssueState::Open) => PRState::Open,
                    Some(octocrab::models::IssueState::Closed) => PRState::Closed,
                    _ => PRState::Open,
                }
            };

            // If filtering for merged, skip non-merged closed PRs
            if filter.state == Some(PRState::Merged) && pr.merged_at.is_none() {
                continue;
            }

            result.push(PullRequest {
                number: pr.number,
                url: pr
                    .html_url
                    .as_ref()
                    .map(|u| u.to_string())
                    .unwrap_or_default(),
                title: pr.title.clone().unwrap_or_default(),
                body: pr.body.clone().unwrap_or_default(),
                state: pr_state,
                merged: pr.merged_at.is_some(),
                mergeable: pr.mergeable,
                head: PRHead {
                    ref_name: pr.head.ref_field.clone(),
                    sha: pr.head.sha.clone(),
                },
                base: PRBase {
                    ref_name: pr.base.ref_field.clone(),
                },
            });
        }

        Ok(result)
    }

    async fn is_pull_request_approved(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<bool, PlatformError> {
        let reviews = self
            .get_pull_request_reviews(owner, repo, pull_number)
            .await?;

        // Check for at least one approval and no changes requested.
        // State comes from Debug formatting of octocrab's ReviewState enum,
        // which gives title case without underscores (e.g. "Approved", "ChangesRequested").
        let state_matches = |state: &str, target: &str| -> bool {
            let normalized: String = state.chars().filter(|c| *c != '_').collect();
            normalized.eq_ignore_ascii_case(target)
        };
        let has_approval = reviews.iter().any(|r| state_matches(&r.state, "Approved"));
        let has_changes_requested = reviews
            .iter()
            .any(|r| state_matches(&r.state, "ChangesRequested"));

        Ok(has_approval && !has_changes_requested)
    }

    async fn get_pull_request_reviews(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<Vec<PRReview>, PlatformError> {
        let client = self.get_client().await?;

        let reviews = client
            .pulls(owner, repo)
            .list_reviews(pull_number)
            .send()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to get reviews: {}", e)))?;

        Ok(reviews
            .items
            .iter()
            .map(|r| PRReview {
                state: r.state.map(|s| format!("{:?}", s)).unwrap_or_default(),
                user: r.user.as_ref().map(|u| u.login.clone()).unwrap_or_default(),
            })
            .collect())
    }

    async fn get_status_checks(
        &self,
        owner: &str,
        repo: &str,
        ref_name: &str,
    ) -> Result<StatusCheckResult, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        // Try Check Runs API first (newer GitHub Actions)
        let check_runs_url = format!(
            "{}/repos/{}/{}/commits/{}/check-runs",
            base_url, owner, repo, ref_name
        );

        let http_client = Self::http_client();
        let response = http_client
            .get(&check_runs_url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if response.status().is_success() {
            #[derive(serde::Deserialize)]
            struct CheckRunsResponse {
                total_count: i64,
                check_runs: Vec<CheckRun>,
            }

            #[derive(serde::Deserialize)]
            struct CheckRun {
                name: String,
                status: String,
                conclusion: Option<String>,
            }

            let check_runs: CheckRunsResponse = response
                .json()
                .await
                .map_err(|e| PlatformError::ParseError(e.to_string()))?;

            if check_runs.total_count > 0 {
                // Determine overall state from check runs
                let (aggregate_state, statuses): (CheckState, Vec<StatusCheck>) =
                    check_runs.check_runs.into_iter().fold(
                        (CheckState::Success, Vec::new()),
                        |(aggregate_state, mut acc), cr| {
                            let check_state = match cr.conclusion.as_deref() {
                                Some("success") => CheckState::Success,
                                Some("failure") | Some("timed_out") => CheckState::Failure,
                                Some("cancelled") => CheckState::Failure,
                                _ => CheckState::Pending, // "in_progress", "queued", "neutral", or null
                            };

                            // Aggregate: any failure = failure, any pending = pending
                            let new_aggregate = match (aggregate_state, check_state) {
                                (CheckState::Failure, _) => CheckState::Failure,
                                (_, CheckState::Failure) => CheckState::Failure,
                                (CheckState::Pending, _) | (_, CheckState::Pending) => {
                                    CheckState::Pending
                                }
                                (CheckState::Success, CheckState::Success) => CheckState::Success,
                            };

                            acc.push(StatusCheck {
                                context: cr.name.clone(),
                                state: cr.conclusion.unwrap_or(cr.status),
                            });

                            (new_aggregate, acc)
                        },
                    );

                return Ok(StatusCheckResult {
                    state: aggregate_state,
                    statuses,
                });
            }
        }

        // Fallback to legacy status checks API
        let status_url = format!(
            "{}/repos/{}/{}/commits/{}/status",
            base_url, owner, repo, ref_name
        );

        let response = http_client
            .get(&status_url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if !response.status().is_success() {
            return Err(PlatformError::ApiError(format!(
                "Failed to get status: {}",
                response.status()
            )));
        }

        #[derive(serde::Deserialize)]
        struct CombinedStatus {
            state: String,
            statuses: Vec<StatusEntry>,
        }

        #[derive(serde::Deserialize)]
        struct StatusEntry {
            context: Option<String>,
            state: String,
        }

        let status: CombinedStatus = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        let state = match status.state.as_str() {
            "success" => CheckState::Success,
            "failure" | "error" => CheckState::Failure,
            _ => CheckState::Pending,
        };

        let statuses = status
            .statuses
            .iter()
            .map(|s| StatusCheck {
                context: s.context.clone().unwrap_or_default(),
                state: s.state.clone(),
            })
            .collect();

        Ok(StatusCheckResult { state, statuses })
    }

    async fn get_allowed_merge_methods(
        &self,
        owner: &str,
        repo: &str,
    ) -> Result<AllowedMergeMethods, PlatformError> {
        let client = self.get_client().await?;

        let repo_info = client
            .repos(owner, repo)
            .get()
            .await
            .map_err(|e| PlatformError::ApiError(format!("Failed to get repo: {}", e)))?;

        Ok(AllowedMergeMethods {
            merge: repo_info.allow_merge_commit.unwrap_or(true),
            squash: repo_info.allow_squash_merge.unwrap_or(true),
            rebase: repo_info.allow_rebase_merge.unwrap_or(true),
        })
    }

    async fn get_pull_request_diff(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<String, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!(
            "{}/repos/{}/{}/pulls/{}",
            base_url, owner, repo, pull_number
        );

        let client = Self::http_client();
        let response = client
            .get(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3.diff")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if !response.status().is_success() {
            return Err(PlatformError::ApiError(format!(
                "Failed to get diff: {}",
                response.status()
            )));
        }

        response
            .text()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))
    }

    async fn list_issues(
        &self,
        owner: &str,
        repo: &str,
        filter: &IssueListFilter,
    ) -> Result<Vec<Issue>, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");
        let http_client = Self::http_client();

        let limit = filter.limit.unwrap_or(30).min(100) as usize;

        // Build base query params (without pagination)
        let mut base_params = Vec::new();
        let state_str = filter
            .state
            .map(|s| s.to_string())
            .unwrap_or_else(|| "all".to_string());
        base_params.push(format!("state={}", state_str));

        if !filter.labels.is_empty() {
            let encoded: Vec<String> = filter
                .labels
                .iter()
                .map(|l| urlencoding::encode(l).into_owned())
                .collect();
            base_params.push(format!("labels={}", encoded.join(",")));
        }
        if let Some(ref assignee) = filter.assignee {
            base_params.push(format!("assignee={}", urlencoding::encode(assignee)));
        }

        // Paginate to fill the requested limit, since GitHub's issues endpoint
        // returns PRs mixed with issues and we filter PRs client-side.
        let mut issues = Vec::new();
        let mut page = 1u32;
        let per_page = 100; // Fetch max per page to minimize round-trips
        let max_pages = 5; // Safety cap to avoid runaway pagination

        loop {
            let mut params = base_params.clone();
            params.push(format!("per_page={}", per_page));
            params.push(format!("page={}", page));

            let url = format!(
                "{}/repos/{}/{}/issues?{}",
                base_url,
                owner,
                repo,
                params.join("&")
            );

            let response = http_client
                .get(&url)
                .header("Authorization", format!("Bearer {}", token))
                .header("Accept", "application/vnd.github.v3+json")
                .header("User-Agent", "gitgrip")
                .send()
                .await
                .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

            check_response_rate_limit(response.headers(), "GitHub").await;

            if !response.status().is_success() {
                let status = response.status();
                let error_text = response.text().await.unwrap_or_default();
                return Err(PlatformError::ApiError(format!(
                    "Failed to list issues ({}): {}",
                    status, error_text
                )));
            }

            let gh_issues: Vec<GhIssueDetail> = response
                .json()
                .await
                .map_err(|e| PlatformError::ParseError(e.to_string()))?;

            let fetched_count = gh_issues.len();

            // Filter out pull requests (GitHub returns PRs in the issues endpoint)
            for item in gh_issues {
                if item.pull_request.is_none() {
                    issues.push(item.into_issue());
                    if issues.len() >= limit {
                        return Ok(issues);
                    }
                }
            }

            // Stop if this page was incomplete (no more results) or we hit the page cap
            if fetched_count < per_page || page >= max_pages {
                break;
            }
            page += 1;
        }

        Ok(issues)
    }

    async fn create_issue(
        &self,
        owner: &str,
        repo: &str,
        options: &IssueCreateOptions,
    ) -> Result<IssueCreateResult, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!("{}/repos/{}/{}/issues", base_url, owner, repo);

        let mut body = serde_json::json!({
            "title": options.title,
        });
        if let Some(ref desc) = options.body {
            body["body"] = serde_json::Value::String(desc.clone());
        }
        if !options.labels.is_empty() {
            body["labels"] = serde_json::json!(options.labels);
        }
        if !options.assignees.is_empty() {
            body["assignees"] = serde_json::json!(options.assignees);
        }

        let http_client = Self::http_client();
        let response = http_client
            .post(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&body)
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to create issue ({}): {}",
                status, error_text
            )));
        }

        #[derive(serde::Deserialize)]
        struct GhIssueResponse {
            number: u64,
            html_url: String,
        }

        let issue: GhIssueResponse = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        Ok(IssueCreateResult {
            number: issue.number,
            url: issue.html_url,
        })
    }

    async fn get_issue(
        &self,
        owner: &str,
        repo: &str,
        issue_number: u64,
    ) -> Result<Issue, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!(
            "{}/repos/{}/{}/issues/{}",
            base_url, owner, repo, issue_number
        );

        let http_client = Self::http_client();
        let response = http_client
            .get(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if response.status().as_u16() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Issue #{} not found",
                issue_number
            )));
        }

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to get issue ({}): {}",
                status, error_text
            )));
        }

        let i: GhIssueDetail = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        // GitHub returns PRs via the issues endpoint; reject them
        if i.pull_request.is_some() {
            return Err(PlatformError::NotFound(format!(
                "#{} is a pull request, not an issue",
                issue_number
            )));
        }

        Ok(i.into_issue())
    }

    async fn close_issue(
        &self,
        owner: &str,
        repo: &str,
        issue_number: u64,
    ) -> Result<(), PlatformError> {
        // Guard: ensure the number refers to an issue, not a PR
        self.get_issue(owner, repo, issue_number).await?;

        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!(
            "{}/repos/{}/{}/issues/{}",
            base_url, owner, repo, issue_number
        );

        let http_client = Self::http_client();
        let response = http_client
            .patch(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&serde_json::json!({ "state": "closed" }))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if response.status().as_u16() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Issue #{} not found",
                issue_number
            )));
        }

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to close issue ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }

    async fn reopen_issue(
        &self,
        owner: &str,
        repo: &str,
        issue_number: u64,
    ) -> Result<(), PlatformError> {
        // Guard: ensure the number refers to an issue, not a PR
        self.get_issue(owner, repo, issue_number).await?;

        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let url = format!(
            "{}/repos/{}/{}/issues/{}",
            base_url, owner, repo, issue_number
        );

        let http_client = Self::http_client();
        let response = http_client
            .patch(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&serde_json::json!({ "state": "open" }))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if response.status().as_u16() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Issue #{} not found",
                issue_number
            )));
        }

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to reopen issue ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }

    fn parse_repo_url(&self, url: &str) -> Option<ParsedRepoInfo> {
        // SSH format: git@github.com:owner/repo.git
        if url.starts_with("git@github.com:") {
            let path = url.trim_start_matches("git@github.com:");
            let path = path.trim_end_matches(".git");
            let parts: Vec<&str> = path.split('/').collect();
            if parts.len() >= 2 {
                return Some(ParsedRepoInfo {
                    owner: parts[0].to_string(),
                    repo: parts[parts.len() - 1].to_string(),
                    project: None,
                    platform: Some(PlatformType::GitHub),
                });
            }
        }

        // HTTPS format: https://github.com/owner/repo.git
        if url.contains("github.com") {
            let url = url.trim_end_matches(".git");
            let parts: Vec<&str> = url.split('/').collect();
            if parts.len() >= 2 {
                let owner_idx = parts.iter().position(|&p| p == "github.com")? + 1;
                if owner_idx + 1 < parts.len() {
                    return Some(ParsedRepoInfo {
                        owner: parts[owner_idx].to_string(),
                        repo: parts[owner_idx + 1].to_string(),
                        project: None,
                        platform: Some(PlatformType::GitHub),
                    });
                }
            }
        }

        None
    }

    fn matches_url(&self, url: &str) -> bool {
        url.contains("github.com")
    }

    async fn create_repository(
        &self,
        owner: &str,
        name: &str,
        description: Option<&str>,
        private: bool,
    ) -> Result<String, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        // Check if owner is the authenticated user or an org
        // First, get the authenticated user
        let http_client = Self::http_client();

        let user_response = http_client
            .get(format!("{}/user", base_url))
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(user_response.headers(), "GitHub").await;

        #[derive(serde::Deserialize)]
        struct User {
            login: String,
        }

        let current_user: User = user_response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        // Determine the API endpoint based on whether owner is the user or an org
        let url = if owner.eq_ignore_ascii_case(&current_user.login) {
            format!("{}/user/repos", base_url)
        } else {
            format!("{}/orgs/{}/repos", base_url, owner)
        };

        #[derive(serde::Serialize)]
        struct CreateRepoRequest {
            name: String,
            #[serde(skip_serializing_if = "Option::is_none")]
            description: Option<String>,
            private: bool,
            auto_init: bool,
        }

        let response = http_client
            .post(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&CreateRepoRequest {
                name: name.to_string(),
                description: description.map(|s| s.to_string()),
                private,
                auto_init: true, // Initialize with a README so there's a default branch
            })
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to create repository ({}): {}",
                status, error_text
            )));
        }

        #[derive(serde::Deserialize)]
        struct RepoResponse {
            ssh_url: String,
        }

        let repo: RepoResponse = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        Ok(repo.ssh_url)
    }

    async fn delete_repository(&self, owner: &str, name: &str) -> Result<(), PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");

        let http_client = Self::http_client();
        let url = format!("{}/repos/{}/{}", base_url, owner, name);

        let response = http_client
            .delete(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if response.status() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Repository {}/{} not found",
                owner, name
            )));
        }

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to delete repository ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }

    async fn create_release(
        &self,
        owner: &str,
        repo: &str,
        tag: &str,
        name: &str,
        body: Option<&str>,
        target_commitish: &str,
        draft: bool,
        prerelease: bool,
    ) -> Result<ReleaseResult, PlatformError> {
        let token = self.get_token().await?;
        let base_url = self.base_url.as_deref().unwrap_or("https://api.github.com");
        let http_client = Self::http_client();

        #[derive(serde::Serialize)]
        struct CreateReleaseRequest {
            tag_name: String,
            target_commitish: String,
            name: String,
            #[serde(skip_serializing_if = "Option::is_none")]
            body: Option<String>,
            draft: bool,
            prerelease: bool,
        }

        let url = format!("{}/repos/{}/{}/releases", base_url, owner, repo);
        let response = http_client
            .post(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Accept", "application/vnd.github.v3+json")
            .header("User-Agent", "gitgrip")
            .json(&CreateReleaseRequest {
                tag_name: tag.to_string(),
                target_commitish: target_commitish.to_string(),
                name: name.to_string(),
                body: body.map(|s| s.to_string()),
                draft,
                prerelease,
            })
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        check_response_rate_limit(response.headers(), "GitHub").await;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to create release ({}): {}",
                status, error_text
            )));
        }

        #[derive(serde::Deserialize)]
        struct ReleaseResponse {
            id: u64,
            tag_name: String,
            html_url: String,
        }

        let release: ReleaseResponse = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        Ok(ReleaseResult {
            id: release.id,
            tag: release.tag_name,
            url: release.html_url,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::super::traits::LinkedPRRef;
    use super::*;

    #[test]
    fn test_parse_github_ssh_url() {
        let adapter = GitHubAdapter::new(None);

        let result = adapter.parse_repo_url("git@github.com:user/repo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "user");
        assert_eq!(info.repo, "repo");
    }

    #[test]
    fn test_parse_github_https_url() {
        let adapter = GitHubAdapter::new(None);

        let result = adapter.parse_repo_url("https://github.com/user/repo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "user");
        assert_eq!(info.repo, "repo");
    }

    #[test]
    fn test_matches_url() {
        let adapter = GitHubAdapter::new(None);

        assert!(adapter.matches_url("git@github.com:user/repo.git"));
        assert!(adapter.matches_url("https://github.com/user/repo.git"));
        assert!(!adapter.matches_url("git@gitlab.com:user/repo.git"));
    }

    #[test]
    fn test_linked_pr_comment_roundtrip() {
        let adapter = GitHubAdapter::new(None);

        let links = vec![
            LinkedPRRef {
                repo_name: "frontend".to_string(),
                number: 42,
            },
            LinkedPRRef {
                repo_name: "backend".to_string(),
                number: 123,
            },
        ];

        let comment = adapter.generate_linked_pr_comment(&links);
        let parsed = adapter.parse_linked_pr_comment(&comment);

        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].repo_name, "frontend");
        assert_eq!(parsed[0].number, 42);
        assert_eq!(parsed[1].repo_name, "backend");
        assert_eq!(parsed[1].number, 123);
    }

    #[test]
    fn test_parse_github_https_url_without_git_suffix() {
        let adapter = GitHubAdapter::new(None);
        let result = adapter
            .parse_repo_url("https://github.com/org/project")
            .unwrap();
        assert_eq!(result.owner, "org");
        assert_eq!(result.repo, "project");
        assert_eq!(result.platform, Some(PlatformType::GitHub));
    }

    #[test]
    fn test_parse_non_github_url_returns_none() {
        let adapter = GitHubAdapter::new(None);
        assert!(adapter
            .parse_repo_url("git@gitlab.com:user/repo.git")
            .is_none());
        assert!(adapter
            .parse_repo_url("https://bitbucket.org/team/repo.git")
            .is_none());
    }

    #[test]
    fn test_generate_linked_pr_comment_empty() {
        let adapter = GitHubAdapter::new(None);
        assert_eq!(adapter.generate_linked_pr_comment(&[]), "");
    }

    #[test]
    fn test_parse_linked_pr_comment_empty_body() {
        let adapter = GitHubAdapter::new(None);
        assert!(adapter.parse_linked_pr_comment("").is_empty());
    }

    #[test]
    fn test_parse_linked_pr_comment_no_end_marker() {
        let adapter = GitHubAdapter::new(None);
        let body = "<!-- gitgrip-linked-prs\napp:42\n";
        assert!(adapter.parse_linked_pr_comment(body).is_empty());
    }

    #[test]
    fn test_parse_linked_pr_comment_malformed_entries() {
        let adapter = GitHubAdapter::new(None);
        let body = "<!-- gitgrip-linked-prs\nno-colon\napp:notanumber\nvalid:42\n-->";
        let links = adapter.parse_linked_pr_comment(body);
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].repo_name, "valid");
        assert_eq!(links[0].number, 42);
    }

    #[test]
    fn test_platform_type() {
        let adapter = GitHubAdapter::new(None);
        assert_eq!(adapter.platform_type(), PlatformType::GitHub);
    }

    #[test]
    fn test_new_with_base_url() {
        let adapter = GitHubAdapter::new(Some("https://github.example.com/api/v3"));
        assert_eq!(
            adapter.base_url,
            Some("https://github.example.com/api/v3".to_string())
        );
    }

    #[test]
    fn test_generate_linked_pr_comment_single() {
        let adapter = GitHubAdapter::new(None);
        let links = vec![LinkedPRRef {
            repo_name: "app".to_string(),
            number: 1,
        }];
        let comment = adapter.generate_linked_pr_comment(&links);
        assert!(comment.contains("<!-- gitgrip-linked-prs"));
        assert!(comment.contains("app:1"));
        assert!(comment.contains("-->"));
    }
}
