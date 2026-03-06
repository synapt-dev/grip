//! Azure DevOps platform adapter

use async_trait::async_trait;
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::env;
use std::time::Duration;

use super::traits::{HostingPlatform, PlatformError};
use super::types::*;
use crate::core::manifest::PlatformType;
use tracing::debug;

/// Default connection timeout in seconds
const CONNECT_TIMEOUT_SECS: u64 = 10;
/// Default request timeout in seconds
const REQUEST_TIMEOUT_SECS: u64 = 30;

/// Azure DevOps context parsed from URL
#[derive(Debug, Clone)]
struct AzureContext {
    organization: String,
    project: String,
    repository: String,
}

/// Azure DevOps pull request response
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AzurePullRequest {
    pull_request_id: u64,
    title: String,
    description: Option<String>,
    status: String, // active, abandoned, completed
    merge_status: Option<String>,
    source_ref_name: String,
    target_ref_name: String,
    last_merge_source_commit: Option<AzureCommit>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AzureCommit {
    commit_id: String,
}

/// Azure DevOps reviewer
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AzureReviewer {
    vote: i32, // 10 = approved, -10 = rejected, 0 = no vote, 5 = approved with suggestions, -5 = waiting for author
    display_name: Option<String>,
    unique_name: Option<String>,
}

/// Azure DevOps build
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AzureBuild {
    result: Option<String>,
    status: String,
}

/// List response wrapper
#[derive(Debug, Deserialize)]
struct ListResponse<T> {
    value: Vec<T>,
}

/// PR with reviewers
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AzurePullRequestWithReviewers {
    reviewers: Option<Vec<AzureReviewer>>,
}

/// Azure DevOps API adapter
pub struct AzureDevOpsAdapter {
    base_url: String,
    http_client: Client,
}

impl AzureDevOpsAdapter {
    /// Create a new Azure DevOps adapter
    pub fn new(base_url: Option<&str>) -> Self {
        let http_client = Client::builder()
            .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
            .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
            .build()
            .unwrap_or_else(|_| Client::new());

        Self {
            base_url: base_url.unwrap_or("https://dev.azure.com").to_string(),
            http_client,
        }
    }

    /// Parse Azure DevOps context from owner string
    /// Format: "org/project" where owner is org and repo is separate
    fn parse_context(&self, owner: &str, repo: &str) -> AzureContext {
        let parts: Vec<&str> = owner.split('/').collect();
        if parts.len() >= 2 {
            AzureContext {
                organization: parts[0].to_string(),
                project: parts[1..].join("/"),
                repository: repo.to_string(),
            }
        } else {
            // Fallback: owner is org, use repo as both project and repo
            AzureContext {
                organization: owner.to_string(),
                project: repo.to_string(),
                repository: repo.to_string(),
            }
        }
    }

    /// Make authenticated API request
    async fn api_request<T: for<'de> Deserialize<'de>>(
        &self,
        method: reqwest::Method,
        ctx: &AzureContext,
        endpoint: &str,
        body: Option<impl Serialize>,
    ) -> Result<T, PlatformError> {
        let token = self.get_token().await?;
        let url = format!(
            "{}/{}/{}/_apis{}?api-version=7.0",
            self.base_url, ctx.organization, ctx.project, endpoint
        );

        let mut request = self
            .http_client
            .request(method, &url)
            .header("Authorization", self.build_auth_header(&token))
            .header("Content-Type", "application/json")
            // Required for Azure AD tokens with Microsoft personal accounts (MSA)
            .header("X-VSS-ForceMsaPassThrough", "true")
            .header("X-TFS-FedAuthRedirect", "Suppress");

        if let Some(b) = body {
            request = request.json(&b);
        }

        let response = request
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Azure DevOps API error ({}): {}",
                status, error_text
            )));
        }

        response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))
    }

    /// Make PATCH request (used for updates)
    async fn api_patch(
        &self,
        ctx: &AzureContext,
        endpoint: &str,
        body: impl Serialize,
    ) -> Result<(), PlatformError> {
        let token = self.get_token().await?;
        let url = format!(
            "{}/{}/{}/_apis{}?api-version=7.0",
            self.base_url, ctx.organization, ctx.project, endpoint
        );

        let response = self
            .http_client
            .patch(&url)
            .header("Authorization", self.build_auth_header(&token))
            .header("Content-Type", "application/json")
            .header("X-VSS-ForceMsaPassThrough", "true")
            .header("X-TFS-FedAuthRedirect", "Suppress")
            .json(&body)
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Azure DevOps API error ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }

    /// Build auth header: Bearer for Azure AD JWT tokens, Basic for PATs.
    fn build_auth_header(&self, token: &str) -> String {
        if token.starts_with("eyJ") {
            format!("Bearer {}", token)
        } else {
            let encoded = STANDARD.encode(format!(":{}", token));
            format!("Basic {}", encoded)
        }
    }

    /// Build PR web URL
    fn build_pr_url(&self, ctx: &AzureContext, pr_id: u64) -> String {
        format!(
            "{}/{}/{}/_git/{}/pullrequest/{}",
            self.base_url, ctx.organization, ctx.project, ctx.repository, pr_id
        )
    }
}

#[async_trait]
impl HostingPlatform for AzureDevOpsAdapter {
    fn platform_type(&self) -> PlatformType {
        PlatformType::AzureDevOps
    }

    async fn get_token(&self) -> Result<String, PlatformError> {
        // Try environment variables first
        if let Ok(token) = env::var("AZURE_DEVOPS_TOKEN") {
            return Ok(token);
        }
        if let Ok(token) = env::var("AZURE_DEVOPS_EXT_PAT") {
            return Ok(token);
        }

        // Try az CLI to get access token
        debug!(target: "gitgrip::cmd", program = "az", args = ?["account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798", "--query", "accessToken", "-o", "tsv"], "exec");
        let output = tokio::process::Command::new("az")
            .args([
                "account",
                "get-access-token",
                "--resource",
                "499b84ac-1321-427f-aa17-267ca6975798",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ])
            .output()
            .await;

        if let Ok(output) = output {
            if output.status.success() {
                let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !token.is_empty() {
                    return Ok(token);
                }
            }
        }

        Err(PlatformError::AuthError(
            "Azure DevOps token not found. Set AZURE_DEVOPS_TOKEN or use 'az login'".to_string(),
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
        let ctx = self.parse_context(owner, repo);

        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct CreatePR {
            source_ref_name: String,
            target_ref_name: String,
            title: String,
            description: String,
            is_draft: bool,
        }

        let pr: AzurePullRequest = self
            .api_request(
                reqwest::Method::POST,
                &ctx,
                &format!("/git/repositories/{}/pullrequests", ctx.repository),
                Some(CreatePR {
                    source_ref_name: format!("refs/heads/{}", head),
                    target_ref_name: format!("refs/heads/{}", base),
                    title: title.to_string(),
                    description: body.unwrap_or("").to_string(),
                    is_draft: draft,
                }),
            )
            .await?;

        Ok(PRCreateResult {
            number: pr.pull_request_id,
            url: self.build_pr_url(&ctx, pr.pull_request_id),
        })
    }

    async fn get_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<PullRequest, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        let pr: AzurePullRequest = self
            .api_request(
                reqwest::Method::GET,
                &ctx,
                &format!(
                    "/git/repositories/{}/pullrequests/{}",
                    ctx.repository, pull_number
                ),
                None::<()>,
            )
            .await?;

        // Map Azure DevOps status to our unified state
        let (state, merged) = match pr.status.as_str() {
            "completed" => {
                let merged = pr.merge_status.as_deref() == Some("succeeded");
                (
                    if merged {
                        PRState::Merged
                    } else {
                        PRState::Closed
                    },
                    merged,
                )
            }
            "abandoned" => (PRState::Closed, false),
            _ => (PRState::Open, false),
        };

        let mergeable = matches!(
            pr.merge_status.as_deref(),
            Some("succeeded") | Some("queued")
        );

        Ok(PullRequest {
            number: pr.pull_request_id,
            url: self.build_pr_url(&ctx, pr.pull_request_id),
            title: pr.title,
            body: pr.description.unwrap_or_default(),
            state,
            merged,
            mergeable: Some(mergeable),
            head: PRHead {
                ref_name: pr.source_ref_name.replace("refs/heads/", ""),
                sha: pr
                    .last_merge_source_commit
                    .map(|c| c.commit_id)
                    .unwrap_or_default(),
            },
            base: PRBase {
                ref_name: pr.target_ref_name.replace("refs/heads/", ""),
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
        let ctx = self.parse_context(owner, repo);

        #[derive(Serialize)]
        struct UpdateBody {
            description: String,
        }

        self.api_patch(
            &ctx,
            &format!(
                "/git/repositories/{}/pullrequests/{}",
                ctx.repository, pull_number
            ),
            UpdateBody {
                description: body.to_string(),
            },
        )
        .await
    }

    async fn merge_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        method: Option<MergeMethod>,
        delete_branch: bool,
    ) -> Result<bool, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        // Get current PR to get the last merge source commit
        let pr = self.get_pull_request(owner, repo, pull_number).await?;

        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct CompletionOptions {
            #[serde(skip_serializing_if = "Option::is_none")]
            delete_source_branch: Option<bool>,
            #[serde(skip_serializing_if = "Option::is_none")]
            merge_strategy: Option<String>,
            #[serde(skip_serializing_if = "Option::is_none")]
            squash_merge: Option<bool>,
        }

        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct CompletePR {
            status: String,
            last_merge_source_commit: LastMergeCommit,
            completion_options: CompletionOptions,
        }

        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct LastMergeCommit {
            commit_id: String,
        }

        let mut completion_options = CompletionOptions {
            delete_source_branch: if delete_branch { Some(true) } else { None },
            merge_strategy: None,
            squash_merge: None,
        };

        match method {
            Some(MergeMethod::Squash) => {
                completion_options.squash_merge = Some(true);
            }
            Some(MergeMethod::Rebase) => {
                completion_options.merge_strategy = Some("rebase".to_string());
            }
            _ => {}
        }

        let result = self
            .api_patch(
                &ctx,
                &format!(
                    "/git/repositories/{}/pullrequests/{}",
                    ctx.repository, pull_number
                ),
                CompletePR {
                    status: "completed".to_string(),
                    last_merge_source_commit: LastMergeCommit {
                        commit_id: pr.head.sha,
                    },
                    completion_options,
                },
            )
            .await;

        match result {
            Ok(()) => Ok(true),
            Err(_) => Ok(false),
        }
    }

    async fn find_pr_by_branch(
        &self,
        owner: &str,
        repo: &str,
        branch: &str,
    ) -> Result<Option<PRCreateResult>, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        let response: ListResponse<AzurePullRequest> = self
            .api_request(
                reqwest::Method::GET,
                &ctx,
                &format!(
                    "/git/repositories/{}/pullrequests?searchCriteria.sourceRefName=refs/heads/{}&searchCriteria.status=active",
                    ctx.repository,
                    urlencoding::encode(branch)
                ),
                None::<()>,
            )
            .await?;

        if let Some(pr) = response.value.first() {
            Ok(Some(PRCreateResult {
                number: pr.pull_request_id,
                url: self.build_pr_url(&ctx, pr.pull_request_id),
            }))
        } else {
            Ok(None)
        }
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

        // Azure DevOps: vote 10 = approved, 5 = approved with suggestions
        let has_approval = reviews
            .iter()
            .any(|r| r.state == "APPROVED" || r.state == "APPROVED_WITH_SUGGESTIONS");
        let has_rejection = reviews
            .iter()
            .any(|r| r.state == "REJECTED" || r.state == "WAITING_FOR_AUTHOR");

        Ok(has_approval && !has_rejection)
    }

    async fn get_pull_request_reviews(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<Vec<PRReview>, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        let pr: AzurePullRequestWithReviewers = self
            .api_request(
                reqwest::Method::GET,
                &ctx,
                &format!(
                    "/git/repositories/{}/pullrequests/{}",
                    ctx.repository, pull_number
                ),
                None::<()>,
            )
            .await?;

        let reviewers = pr.reviewers.unwrap_or_default();

        Ok(reviewers
            .iter()
            .map(|r| {
                let state = match r.vote {
                    10 => "APPROVED",
                    5 => "APPROVED_WITH_SUGGESTIONS",
                    -10 => "REJECTED",
                    -5 => "WAITING_FOR_AUTHOR",
                    _ => "PENDING",
                };
                PRReview {
                    state: state.to_string(),
                    user: r
                        .display_name
                        .clone()
                        .or_else(|| r.unique_name.clone())
                        .unwrap_or_default(),
                }
            })
            .collect())
    }

    async fn get_status_checks(
        &self,
        owner: &str,
        repo: &str,
        _ref_name: &str,
    ) -> Result<StatusCheckResult, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        // Get builds for this repository
        let result: Result<ListResponse<AzureBuild>, _> = self
            .api_request(
                reqwest::Method::GET,
                &ctx,
                &format!(
                    "/build/builds?repositoryId={}&repositoryType=TfsGit&$top=5",
                    ctx.repository
                ),
                None::<()>,
            )
            .await;

        match result {
            Ok(response) => {
                if response.value.is_empty() {
                    return Ok(StatusCheckResult {
                        state: CheckState::Success,
                        statuses: vec![],
                    });
                }

                let has_failure = response
                    .value
                    .iter()
                    .any(|b| matches!(b.result.as_deref(), Some("failed") | Some("canceled")));
                let has_in_progress = response.value.iter().any(|b| b.status != "completed");

                let state = if has_failure {
                    CheckState::Failure
                } else if has_in_progress {
                    CheckState::Pending
                } else {
                    CheckState::Success
                };

                let statuses = response
                    .value
                    .iter()
                    .map(|b| StatusCheck {
                        context: "azure-pipeline".to_string(),
                        state: b.result.clone().unwrap_or_else(|| b.status.clone()),
                    })
                    .collect();

                Ok(StatusCheckResult { state, statuses })
            }
            Err(_) => {
                // No builds or API error - assume success
                Ok(StatusCheckResult {
                    state: CheckState::Success,
                    statuses: vec![],
                })
            }
        }
    }

    async fn get_allowed_merge_methods(
        &self,
        _owner: &str,
        _repo: &str,
    ) -> Result<AllowedMergeMethods, PlatformError> {
        // Azure DevOps generally allows all merge methods
        Ok(AllowedMergeMethods {
            merge: true,
            squash: true,
            rebase: true,
        })
    }

    async fn get_pull_request_diff(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<String, PlatformError> {
        let ctx = self.parse_context(owner, repo);

        let endpoint = format!(
            "/git/repositories/{}/pullRequests/{}/commits",
            ctx.repository, pull_number
        );

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct CommitInfo {
            commit_id: String,
            comment: Option<String>,
        }

        #[derive(Deserialize)]
        struct CommitsResponse {
            value: Vec<CommitInfo>,
        }

        let commits: CommitsResponse = self
            .api_request(reqwest::Method::GET, &ctx, &endpoint, None::<()>)
            .await?;

        // Build a summary of commits
        let mut diff = String::new();
        diff.push_str(&format!(
            "Pull Request #{} - {} commits\n\n",
            pull_number,
            commits.value.len()
        ));
        for commit in &commits.value {
            diff.push_str(&format!(
                "{}: {}\n",
                &commit.commit_id[..8.min(commit.commit_id.len())],
                commit.comment.as_deref().unwrap_or("(no message)")
            ));
        }

        Ok(diff)
    }

    fn parse_repo_url(&self, url: &str) -> Option<ParsedRepoInfo> {
        // SSH format: git@ssh.dev.azure.com:v3/org/project/repo
        if let Some(caps) = url.strip_prefix("git@ssh.dev.azure.com:v3/") {
            let parts: Vec<&str> = caps.trim_end_matches(".git").split('/').collect();
            if parts.len() >= 3 {
                return Some(ParsedRepoInfo {
                    owner: format!("{}/{}", parts[0], parts[1]),
                    repo: parts[2].to_string(),
                    project: Some(parts[1].to_string()),
                    platform: Some(PlatformType::AzureDevOps),
                });
            }
        }

        // HTTPS format: https://dev.azure.com/org/project/_git/repo
        if url.contains("dev.azure.com") {
            let parts: Vec<&str> = url.split('/').collect();
            if let Some(git_idx) = parts.iter().position(|&p| p == "_git") {
                if git_idx >= 2 && git_idx + 1 < parts.len() {
                    let org_idx = parts.iter().position(|&p| p == "dev.azure.com")? + 1;
                    if org_idx + 1 < git_idx {
                        return Some(ParsedRepoInfo {
                            owner: format!("{}/{}", parts[org_idx], parts[org_idx + 1]),
                            repo: parts[git_idx + 1].trim_end_matches(".git").to_string(),
                            project: Some(parts[org_idx + 1].to_string()),
                            platform: Some(PlatformType::AzureDevOps),
                        });
                    }
                }
            }
        }

        // Legacy visualstudio.com format: https://org.visualstudio.com/project/_git/repo
        if url.contains(".visualstudio.com") {
            let parts: Vec<&str> = url.split('/').collect();
            if let Some(git_idx) = parts.iter().position(|&p| p == "_git") {
                if git_idx >= 2 && git_idx + 1 < parts.len() {
                    // Extract org from hostname
                    if let Some(host_part) = parts.iter().find(|p| p.contains(".visualstudio.com"))
                    {
                        let org = host_part.split('.').next()?;
                        let project = parts[git_idx - 1];
                        return Some(ParsedRepoInfo {
                            owner: format!("{}/{}", org, project),
                            repo: parts[git_idx + 1].trim_end_matches(".git").to_string(),
                            project: Some(project.to_string()),
                            platform: Some(PlatformType::AzureDevOps),
                        });
                    }
                }
            }
        }

        None
    }

    fn matches_url(&self, url: &str) -> bool {
        url.contains("dev.azure.com")
            || url.contains("visualstudio.com")
            || url.contains("ssh.dev.azure.com")
    }

    async fn create_repository(
        &self,
        owner: &str,
        name: &str,
        _description: Option<&str>,
        _private: bool,
    ) -> Result<String, PlatformError> {
        // Azure DevOps owner format: org/project
        let ctx = self.parse_context(owner, name);

        #[derive(Serialize)]
        struct CreateRepoRequest {
            name: String,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct CreateRepoResponse {
            ssh_url: Option<String>,
            remote_url: Option<String>,
        }

        let response: CreateRepoResponse = self
            .api_request(
                reqwest::Method::POST,
                &ctx,
                "/git/repositories",
                Some(CreateRepoRequest {
                    name: name.to_string(),
                }),
            )
            .await?;

        // Prefer SSH URL, fall back to HTTPS
        let clone_url = response.ssh_url.or(response.remote_url).ok_or_else(|| {
            PlatformError::ParseError("No clone URL returned from Azure DevOps".to_string())
        })?;

        Ok(clone_url)
    }

    async fn delete_repository(&self, owner: &str, name: &str) -> Result<(), PlatformError> {
        let ctx = self.parse_context(owner, name);
        let token = self.get_token().await?;

        // First, get the repository ID
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct RepoInfo {
            id: String,
        }

        let repo_info: RepoInfo = self
            .api_request(
                reqwest::Method::GET,
                &ctx,
                &format!("/git/repositories/{}", ctx.repository),
                None::<()>,
            )
            .await?;

        // Delete the repository by ID
        let url = format!(
            "{}/{}/{}/_apis/git/repositories/{}?api-version=7.0",
            self.base_url, ctx.organization, ctx.project, repo_info.id
        );

        let response = self
            .http_client
            .delete(&url)
            .header("Authorization", self.build_auth_header(&token))
            .header("X-VSS-ForceMsaPassThrough", "true")
            .header("X-TFS-FedAuthRedirect", "Suppress")
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if response.status() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Repository {} not found",
                name
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
}

#[cfg(test)]
mod tests {
    use super::super::traits::LinkedPRRef;
    use super::*;

    #[test]
    fn test_parse_azure_ssh_url() {
        let adapter = AzureDevOpsAdapter::new(None);

        let result = adapter.parse_repo_url("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "myorg/myproject");
        assert_eq!(info.repo, "myrepo");
        assert_eq!(info.project, Some("myproject".to_string()));
    }

    #[test]
    fn test_parse_azure_https_url() {
        let adapter = AzureDevOpsAdapter::new(None);

        let result = adapter.parse_repo_url("https://dev.azure.com/myorg/myproject/_git/myrepo");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "myorg/myproject");
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_parse_visualstudio_url() {
        let adapter = AzureDevOpsAdapter::new(None);

        let result = adapter.parse_repo_url("https://myorg.visualstudio.com/myproject/_git/myrepo");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "myorg/myproject");
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_matches_url() {
        let adapter = AzureDevOpsAdapter::new(None);

        assert!(adapter.matches_url("https://dev.azure.com/org/project/_git/repo"));
        assert!(adapter.matches_url("git@ssh.dev.azure.com:v3/org/project/repo"));
        assert!(adapter.matches_url("https://org.visualstudio.com/project/_git/repo"));
        assert!(!adapter.matches_url("https://github.com/user/repo"));
    }

    #[test]
    fn test_linked_pr_comment_roundtrip() {
        let adapter = AzureDevOpsAdapter::new(None);

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
    }

    #[test]
    fn test_parse_context_org_project() {
        let adapter = AzureDevOpsAdapter::new(None);
        let ctx = adapter.parse_context("myorg/myproject", "myrepo");
        assert_eq!(ctx.organization, "myorg");
        assert_eq!(ctx.project, "myproject");
        assert_eq!(ctx.repository, "myrepo");
    }

    #[test]
    fn test_parse_context_org_only() {
        let adapter = AzureDevOpsAdapter::new(None);
        let ctx = adapter.parse_context("myorg", "myrepo");
        assert_eq!(ctx.organization, "myorg");
        assert_eq!(ctx.project, "myrepo");
        assert_eq!(ctx.repository, "myrepo");
    }

    #[test]
    fn test_build_pr_url() {
        let adapter = AzureDevOpsAdapter::new(None);
        let ctx = AzureContext {
            organization: "myorg".to_string(),
            project: "myproject".to_string(),
            repository: "myrepo".to_string(),
        };
        let url = adapter.build_pr_url(&ctx, 42);
        assert_eq!(
            url,
            "https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/42"
        );
    }

    #[test]
    fn test_build_pr_url_custom_base() {
        let adapter = AzureDevOpsAdapter::new(Some("https://azure.example.com"));
        let ctx = AzureContext {
            organization: "org".to_string(),
            project: "proj".to_string(),
            repository: "repo".to_string(),
        };
        let url = adapter.build_pr_url(&ctx, 1);
        assert_eq!(
            url,
            "https://azure.example.com/org/proj/_git/repo/pullrequest/1"
        );
    }

    #[test]
    fn test_parse_azure_ssh_url_with_git_suffix() {
        let adapter = AzureDevOpsAdapter::new(None);
        let result = adapter.parse_repo_url("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_parse_linked_pr_empty_comment() {
        let adapter = AzureDevOpsAdapter::new(None);
        let parsed = adapter.parse_linked_pr_comment("no linked PRs here");
        assert!(parsed.is_empty());
    }

    #[test]
    fn test_generate_linked_pr_empty() {
        let adapter = AzureDevOpsAdapter::new(None);
        let comment = adapter.generate_linked_pr_comment(&[]);
        assert!(comment.is_empty());
    }

    #[test]
    fn test_parse_non_azure_url_returns_none() {
        let adapter = AzureDevOpsAdapter::new(None);
        assert!(adapter
            .parse_repo_url("https://github.com/user/repo")
            .is_none());
    }
}
