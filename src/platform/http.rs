//! Shared HTTP utilities for platform adapters

use reqwest::Client;
use std::time::Duration;
use tracing::debug;

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
