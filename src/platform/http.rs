//! Shared HTTP utilities for platform adapters

use reqwest::header::HeaderMap;
use reqwest::Client;
use std::time::Duration;
use tracing::debug;

use super::rate_limit::{
    check_rate_limit_warning, parse_azure_rate_limits, parse_github_rate_limits,
    parse_gitlab_rate_limits, wait_for_rate_limit,
};

/// Default connection timeout in seconds
const CONNECT_TIMEOUT_SECS: u64 = 10;
/// Default request timeout in seconds
const REQUEST_TIMEOUT_SECS: u64 = 30;

/// Create a configured HTTP client with standard timeouts.
///
/// All platform adapters should use this to ensure consistent timeout behavior.
/// Falls back to a default client if the builder fails.
pub fn create_http_client() -> Client {
    Client::builder()
        .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
        .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
        .build()
        .unwrap_or_else(|err| {
            debug!(
                error = %err,
                "Failed to build HTTP client with timeouts; falling back to default client"
            );
            Client::new()
        })
}

/// Check rate limit headers from an API response and warn if approaching limits.
///
/// Call this after `.send().await` on any platform API request. If rate limited,
/// waits for the reset window before returning.
pub async fn check_response_rate_limit(headers: &HeaderMap, platform: &str) {
    let info = match platform {
        "GitHub" => parse_github_rate_limits(headers),
        "GitLab" => parse_gitlab_rate_limits(headers),
        "Azure DevOps" => parse_azure_rate_limits(headers),
        _ => return,
    };
    check_rate_limit_warning(&info, platform);
    if info.is_rate_limited() {
        wait_for_rate_limit(&info).await;
    }
}
