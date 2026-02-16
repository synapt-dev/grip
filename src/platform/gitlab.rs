//! GitLab platform adapter

use async_trait::async_trait;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::env;
use std::time::Duration;

use super::traits::{HostingPlatform, LinkedPRRef, PlatformError};
use super::types::*;
use crate::core::manifest::PlatformType;
use tracing::debug;

/// Default connection timeout in seconds
const CONNECT_TIMEOUT_SECS: u64 = 10;
/// Default request timeout in seconds
const REQUEST_TIMEOUT_SECS: u64 = 30;

/// GitLab merge request response
#[derive(Debug, Deserialize)]
struct GitLabMergeRequest {
    iid: u64,
    web_url: String,
    title: String,
    description: Option<String>,
    state: String, // opened, closed, merged
    merge_status: Option<String>,
    detailed_merge_status: Option<String>,
    source_branch: String,
    target_branch: String,
    sha: String,
}

/// GitLab approval response
#[derive(Debug, Deserialize)]
struct GitLabApproval {
    approved: bool,
    approved_by: Vec<ApprovedBy>,
}

#[derive(Debug, Deserialize)]
struct ApprovedBy {
    user: ApprovalUser,
}

#[derive(Debug, Deserialize)]
struct ApprovalUser {
    username: String,
}

/// GitLab pipeline
#[derive(Debug, Deserialize)]
struct GitLabPipeline {
    status: String, // success, failed, running, pending, canceled, skipped
}

/// GitLab API adapter
pub struct GitLabAdapter {
    base_url: String,
    http_client: Client,
}

impl GitLabAdapter {
    /// Create a new GitLab adapter
    pub fn new(base_url: Option<&str>) -> Self {
        let http_client = Client::builder()
            .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
            .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
            .build()
            .unwrap_or_else(|_| Client::new());

        Self {
            base_url: base_url.unwrap_or("https://gitlab.com").to_string(),
            http_client,
        }
    }

    /// Encode project path for GitLab API (owner/repo -> owner%2Frepo)
    fn encode_project(&self, owner: &str, repo: &str) -> String {
        urlencoding::encode(&format!("{}/{}", owner, repo)).into_owned()
    }

    /// Get the namespace (group) ID for the given owner/path
    async fn get_namespace_id(&self, owner: &str) -> Result<Option<u64>, PlatformError> {
        let token = self.get_token().await?;
        let encoded_owner = urlencoding::encode(owner);
        let url = format!("{}/api/v4/namespaces/{}", self.base_url, encoded_owner);

        let response = self
            .http_client
            .get(&url)
            .header("Authorization", format!("Bearer {}", token))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if response.status() == 404 {
            // Namespace not found - might be the user's personal namespace
            return Ok(None);
        }

        if !response.status().is_success() {
            // Just return None if we can't find it - GitLab will use the user's namespace
            return Ok(None);
        }

        #[derive(Deserialize)]
        struct Namespace {
            id: u64,
        }

        let namespace: Namespace = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        Ok(Some(namespace.id))
    }

    /// Make authenticated API request
    async fn api_request<T: for<'de> Deserialize<'de>>(
        &self,
        method: reqwest::Method,
        endpoint: &str,
        body: Option<impl Serialize>,
    ) -> Result<T, PlatformError> {
        let token = self.get_token().await?;
        let url = format!("{}/api/v4{}", self.base_url, endpoint);

        let mut request = self
            .http_client
            .request(method, &url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Content-Type", "application/json");

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
                "GitLab API error ({}): {}",
                status, error_text
            )));
        }

        response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))
    }

    /// Make PUT request
    async fn api_put(
        &self,
        endpoint: &str,
        body: Option<impl Serialize>,
    ) -> Result<(), PlatformError> {
        let token = self.get_token().await?;
        let url = format!("{}/api/v4{}", self.base_url, endpoint);

        let mut request = self
            .http_client
            .put(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Content-Type", "application/json");

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
                "GitLab API error ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }
}

#[async_trait]
impl HostingPlatform for GitLabAdapter {
    fn platform_type(&self) -> PlatformType {
        PlatformType::GitLab
    }

    async fn get_token(&self) -> Result<String, PlatformError> {
        // Try environment variable first
        if let Ok(token) = env::var("GITLAB_TOKEN") {
            return Ok(token);
        }

        // Try glab CLI
        debug!(target: "gitgrip::cmd", program = "glab", args = ?["auth", "status", "-t"], "exec");
        let output = tokio::process::Command::new("glab")
            .args(["auth", "status", "-t"])
            .output()
            .await;

        if let Ok(output) = output {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            let combined = format!("{}{}", stdout, stderr);

            // Parse token from glab output: "✓ Token found: glpat-..." or "Token: glpat-..."
            if let Some(token_match) = combined.lines().find(|l| l.contains("Token")) {
                if let Some(token) = token_match.split_whitespace().last() {
                    // Token should start with glpat- or be a hex string
                    if !token.is_empty()
                        && token != "Token:"
                        && token != "Token"
                        && token != "found:"
                    {
                        return Ok(token.to_string());
                    }
                }
            }
        }

        Err(PlatformError::AuthError(
            "GitLab token not found. Set GITLAB_TOKEN or run 'glab auth login'".to_string(),
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
        let project_id = self.encode_project(owner, repo);
        let mr_title = if draft {
            format!("Draft: {}", title)
        } else {
            title.to_string()
        };

        #[derive(Serialize)]
        struct CreateMR {
            source_branch: String,
            target_branch: String,
            title: String,
            description: String,
        }

        let mr: GitLabMergeRequest = self
            .api_request(
                reqwest::Method::POST,
                &format!("/projects/{}/merge_requests", project_id),
                Some(CreateMR {
                    source_branch: head.to_string(),
                    target_branch: base.to_string(),
                    title: mr_title,
                    description: body.unwrap_or("").to_string(),
                }),
            )
            .await?;

        Ok(PRCreateResult {
            number: mr.iid,
            url: mr.web_url,
        })
    }

    async fn get_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<PullRequest, PlatformError> {
        let project_id = self.encode_project(owner, repo);

        let mr: GitLabMergeRequest = self
            .api_request(
                reqwest::Method::GET,
                &format!("/projects/{}/merge_requests/{}", project_id, pull_number),
                None::<()>,
            )
            .await?;

        // Map GitLab state to our unified state
        let (state, merged) = match mr.state.as_str() {
            "merged" => (PRState::Merged, true),
            "closed" => (PRState::Closed, false),
            _ => (PRState::Open, false),
        };

        // GitLab uses detailed_merge_status for mergeability
        let mergeable = mr.detailed_merge_status.as_deref() == Some("mergeable")
            || mr.merge_status.as_deref() == Some("can_be_merged");

        Ok(PullRequest {
            number: mr.iid,
            url: mr.web_url,
            title: mr.title,
            body: mr.description.unwrap_or_default(),
            state,
            merged,
            mergeable: Some(mergeable),
            head: PRHead {
                ref_name: mr.source_branch,
                sha: mr.sha,
            },
            base: PRBase {
                ref_name: mr.target_branch,
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
        let project_id = self.encode_project(owner, repo);

        #[derive(Serialize)]
        struct UpdateBody {
            description: String,
        }

        self.api_put(
            &format!("/projects/{}/merge_requests/{}", project_id, pull_number),
            Some(UpdateBody {
                description: body.to_string(),
            }),
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
        let project_id = self.encode_project(owner, repo);

        #[derive(Serialize)]
        struct MergeParams {
            #[serde(skip_serializing_if = "Option::is_none")]
            squash: Option<bool>,
            #[serde(skip_serializing_if = "Option::is_none")]
            should_remove_source_branch: Option<bool>,
        }

        let params = MergeParams {
            squash: if matches!(method, Some(MergeMethod::Squash)) {
                Some(true)
            } else {
                None
            },
            should_remove_source_branch: if delete_branch { Some(true) } else { None },
        };

        let result = self
            .api_put(
                &format!(
                    "/projects/{}/merge_requests/{}/merge",
                    project_id, pull_number
                ),
                Some(params),
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
        let project_id = self.encode_project(owner, repo);

        let mrs: Vec<GitLabMergeRequest> = self
            .api_request(
                reqwest::Method::GET,
                &format!(
                    "/projects/{}/merge_requests?source_branch={}&state=opened",
                    project_id,
                    urlencoding::encode(branch)
                ),
                None::<()>,
            )
            .await?;

        if let Some(mr) = mrs.first() {
            Ok(Some(PRCreateResult {
                number: mr.iid,
                url: mr.web_url.clone(),
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
        let project_id = self.encode_project(owner, repo);

        let result: Result<GitLabApproval, _> = self
            .api_request(
                reqwest::Method::GET,
                &format!(
                    "/projects/{}/merge_requests/{}/approvals",
                    project_id, pull_number
                ),
                None::<()>,
            )
            .await;

        match result {
            Ok(approval) => Ok(approval.approved),
            Err(_) => {
                // Approvals API might not be available (requires license)
                Ok(false)
            }
        }
    }

    async fn get_pull_request_reviews(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<Vec<PRReview>, PlatformError> {
        let project_id = self.encode_project(owner, repo);

        let result: Result<GitLabApproval, _> = self
            .api_request(
                reqwest::Method::GET,
                &format!(
                    "/projects/{}/merge_requests/{}/approvals",
                    project_id, pull_number
                ),
                None::<()>,
            )
            .await;

        match result {
            Ok(approval) => Ok(approval
                .approved_by
                .iter()
                .map(|a| PRReview {
                    state: "APPROVED".to_string(),
                    user: a.user.username.clone(),
                })
                .collect()),
            Err(_) => Ok(vec![]),
        }
    }

    async fn get_status_checks(
        &self,
        owner: &str,
        repo: &str,
        ref_name: &str,
    ) -> Result<StatusCheckResult, PlatformError> {
        let project_id = self.encode_project(owner, repo);

        let result: Result<Vec<GitLabPipeline>, _> = self
            .api_request(
                reqwest::Method::GET,
                &format!(
                    "/projects/{}/pipelines?sha={}&per_page=1",
                    project_id, ref_name
                ),
                None::<()>,
            )
            .await;

        match result {
            Ok(pipelines) => {
                if pipelines.is_empty() {
                    return Ok(StatusCheckResult {
                        state: CheckState::Success,
                        statuses: vec![],
                    });
                }

                let pipeline = &pipelines[0];

                let state = match pipeline.status.as_str() {
                    "success" => CheckState::Success,
                    "failed" | "canceled" => CheckState::Failure,
                    _ => CheckState::Pending,
                };

                Ok(StatusCheckResult {
                    state,
                    statuses: vec![StatusCheck {
                        context: "gitlab-pipeline".to_string(),
                        state: pipeline.status.clone(),
                    }],
                })
            }
            Err(_) => Ok(StatusCheckResult {
                state: CheckState::Success,
                statuses: vec![],
            }),
        }
    }

    async fn get_allowed_merge_methods(
        &self,
        _owner: &str,
        _repo: &str,
    ) -> Result<AllowedMergeMethods, PlatformError> {
        // GitLab generally allows merge and squash (rebase depends on project settings)
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
        let project_id = self.encode_project(owner, repo);
        let token = self.get_token().await?;

        // Get MR changes
        let url = format!(
            "{}/api/v4/projects/{}/merge_requests/{}/changes",
            self.base_url, project_id, pull_number
        );

        let response = self
            .http_client
            .get(&url)
            .header("Authorization", format!("Bearer {}", token))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if !response.status().is_success() {
            return Err(PlatformError::ApiError(format!(
                "Failed to get MR changes: {}",
                response.status()
            )));
        }

        #[derive(Deserialize)]
        struct Change {
            old_path: String,
            new_path: String,
            diff: String,
        }

        #[derive(Deserialize)]
        struct ChangesResponse {
            changes: Vec<Change>,
        }

        let changes: ChangesResponse = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        let mut diff = String::new();
        for change in &changes.changes {
            diff.push_str(&format!("--- a/{}\n", change.old_path));
            diff.push_str(&format!("+++ b/{}\n", change.new_path));
            diff.push_str(&change.diff);
            diff.push('\n');
        }

        Ok(diff)
    }

    fn parse_repo_url(&self, url: &str) -> Option<ParsedRepoInfo> {
        // SSH format: git@gitlab.com:owner/repo.git or git@gitlab.com:group/subgroup/repo.git
        if url.starts_with("git@") && url.contains("gitlab") {
            let path = url.split(':').nth(1)?;
            let path = path.trim_end_matches(".git");
            let parts: Vec<&str> = path.split('/').collect();
            if parts.len() >= 2 {
                let repo = parts.last()?.to_string();
                let owner = parts[..parts.len() - 1].join("/");
                return Some(ParsedRepoInfo {
                    owner,
                    repo,
                    project: None,
                    platform: Some(PlatformType::GitLab),
                });
            }
        }

        // HTTPS format: https://gitlab.com/owner/repo.git
        if url.contains("gitlab") && url.contains("://") {
            let path = url.split('/').skip(3).collect::<Vec<_>>().join("/");
            let path = path.trim_end_matches(".git");
            let parts: Vec<&str> = path.split('/').collect();
            if parts.len() >= 2 {
                let repo = parts.last()?.to_string();
                let owner = parts[..parts.len() - 1].join("/");
                return Some(ParsedRepoInfo {
                    owner,
                    repo,
                    project: None,
                    platform: Some(PlatformType::GitLab),
                });
            }
        }

        None
    }

    fn matches_url(&self, url: &str) -> bool {
        // Check for gitlab.com
        if url.contains("gitlab.com") {
            return true;
        }

        // Check if URL appears to be GitLab (contains gitlab in hostname)
        if url.contains("://gitlab.") || url.contains("@gitlab.") {
            return true;
        }

        false
    }

    async fn create_repository(
        &self,
        owner: &str,
        name: &str,
        description: Option<&str>,
        private: bool,
    ) -> Result<String, PlatformError> {
        let token = self.get_token().await?;
        let url = format!("{}/api/v4/projects", self.base_url);

        // GitLab visibility levels: private, internal, public
        let visibility = if private { "private" } else { "public" };

        #[derive(Serialize)]
        struct CreateProjectRequest {
            name: String,
            path: String,
            #[serde(skip_serializing_if = "Option::is_none")]
            namespace_id: Option<u64>,
            #[serde(skip_serializing_if = "Option::is_none")]
            description: Option<String>,
            visibility: String,
            initialize_with_readme: bool,
        }

        // Try to get the namespace (group) ID for the owner
        let namespace_id = self.get_namespace_id(owner).await?;

        let response = self
            .http_client
            .post(&url)
            .header("Authorization", format!("Bearer {}", token))
            .header("Content-Type", "application/json")
            .json(&CreateProjectRequest {
                name: name.to_string(),
                path: name.to_string(),
                namespace_id,
                description: description.map(|s| s.to_string()),
                visibility: visibility.to_string(),
                initialize_with_readme: true,
            })
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to create project ({}): {}",
                status, error_text
            )));
        }

        #[derive(Deserialize)]
        struct ProjectResponse {
            ssh_url_to_repo: String,
        }

        let project: ProjectResponse = response
            .json()
            .await
            .map_err(|e| PlatformError::ParseError(e.to_string()))?;

        Ok(project.ssh_url_to_repo)
    }

    async fn delete_repository(&self, owner: &str, name: &str) -> Result<(), PlatformError> {
        let token = self.get_token().await?;
        let project_id = self.encode_project(owner, name);
        let url = format!("{}/api/v4/projects/{}", self.base_url, project_id);

        let response = self
            .http_client
            .delete(&url)
            .header("Authorization", format!("Bearer {}", token))
            .send()
            .await
            .map_err(|e| PlatformError::NetworkError(e.to_string()))?;

        if response.status() == 404 {
            return Err(PlatformError::NotFound(format!(
                "Project {}/{} not found",
                owner, name
            )));
        }

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(PlatformError::ApiError(format!(
                "Failed to delete project ({}): {}",
                status, error_text
            )));
        }

        Ok(())
    }

    fn generate_linked_pr_comment(&self, links: &[LinkedPRRef]) -> String {
        if links.is_empty() {
            return String::new();
        }

        let mut comment = String::from("<!-- gitgrip-linked-prs\n");
        for link in links {
            comment.push_str(&format!("{}:{}\n", link.repo_name, link.number));
        }
        comment.push_str("-->");
        comment
    }

    fn parse_linked_pr_comment(&self, body: &str) -> Vec<LinkedPRRef> {
        let start_marker = "<!-- gitgrip-linked-prs";
        let end_marker = "-->";

        let Some(start) = body.find(start_marker) else {
            return Vec::new();
        };

        let content_start = start + start_marker.len();
        let Some(end) = body[content_start..].find(end_marker) else {
            return Vec::new();
        };

        let content = &body[content_start..content_start + end];

        content
            .lines()
            .filter_map(|line| {
                let line = line.trim();
                if line.is_empty() {
                    return None;
                }

                let parts: Vec<&str> = line.splitn(2, ':').collect();
                if parts.len() != 2 {
                    return None;
                }

                let number = parts[1].parse().ok()?;
                Some(LinkedPRRef {
                    repo_name: parts[0].to_string(),
                    number,
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_gitlab_ssh_url() {
        let adapter = GitLabAdapter::new(None);

        let result = adapter.parse_repo_url("git@gitlab.com:mygroup/myrepo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "mygroup");
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_parse_gitlab_ssh_url_with_subgroup() {
        let adapter = GitLabAdapter::new(None);

        let result = adapter.parse_repo_url("git@gitlab.com:mygroup/subgroup/myrepo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "mygroup/subgroup");
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_parse_gitlab_https_url() {
        let adapter = GitLabAdapter::new(None);

        let result = adapter.parse_repo_url("https://gitlab.com/mygroup/myrepo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "mygroup");
        assert_eq!(info.repo, "myrepo");
    }

    #[test]
    fn test_matches_url() {
        let adapter = GitLabAdapter::new(None);

        assert!(adapter.matches_url("git@gitlab.com:user/repo.git"));
        assert!(adapter.matches_url("https://gitlab.com/user/repo.git"));
        assert!(adapter.matches_url("https://gitlab.company.com/user/repo.git"));
        assert!(!adapter.matches_url("https://github.com/user/repo"));
    }

    #[test]
    fn test_linked_pr_comment_roundtrip() {
        let adapter = GitLabAdapter::new(None);

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
    fn test_encode_project() {
        let adapter = GitLabAdapter::new(None);
        assert_eq!(
            adapter.encode_project("mygroup", "myrepo"),
            "mygroup%2Fmyrepo"
        );
    }

    #[test]
    fn test_encode_project_with_subgroup() {
        let adapter = GitLabAdapter::new(None);
        assert_eq!(
            adapter.encode_project("mygroup/subgroup", "myrepo"),
            "mygroup%2Fsubgroup%2Fmyrepo"
        );
    }

    #[test]
    fn test_parse_self_hosted_gitlab_ssh() {
        let adapter = GitLabAdapter::new(Some("https://gitlab.company.com"));
        let result = adapter.parse_repo_url("git@gitlab.company.com:team/repo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "team");
        assert_eq!(info.repo, "repo");
    }

    #[test]
    fn test_parse_self_hosted_gitlab_https() {
        let adapter = GitLabAdapter::new(Some("https://gitlab.company.com"));
        let result = adapter.parse_repo_url("https://gitlab.company.com/team/repo.git");
        assert!(result.is_some());
        let info = result.unwrap();
        assert_eq!(info.owner, "team");
        assert_eq!(info.repo, "repo");
    }

    #[test]
    fn test_parse_linked_pr_empty_body() {
        let adapter = GitLabAdapter::new(None);
        let parsed = adapter.parse_linked_pr_comment("just a description");
        assert!(parsed.is_empty());
    }

    #[test]
    fn test_parse_linked_pr_unterminated_comment() {
        let adapter = GitLabAdapter::new(None);
        let parsed = adapter.parse_linked_pr_comment("<!-- gitgrip-linked-prs\nfoo:42\n");
        assert!(parsed.is_empty()); // no closing -->
    }

    #[test]
    fn test_generate_linked_pr_empty() {
        let adapter = GitLabAdapter::new(None);
        let comment = adapter.generate_linked_pr_comment(&[]);
        assert!(comment.is_empty());
    }

    #[test]
    fn test_matches_url_non_gitlab() {
        let adapter = GitLabAdapter::new(None);
        assert!(!adapter.matches_url("https://github.com/user/repo"));
        assert!(!adapter.matches_url("git@bitbucket.org:user/repo.git"));
    }

    #[test]
    fn test_parse_non_gitlab_url_returns_none() {
        let adapter = GitLabAdapter::new(None);
        assert!(adapter
            .parse_repo_url("https://github.com/user/repo")
            .is_none());
    }
}
