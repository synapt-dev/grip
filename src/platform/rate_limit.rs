//! Rate limiting detection and handling for platform APIs

use crate::cli::output::Output;
use chrono::{DateTime, Utc};
use reqwest::header::HeaderMap;
use std::time::Duration;

/// Rate limit information parsed from API response headers
#[derive(Debug, Clone)]
pub struct RateLimitInfo {
    /// Remaining requests in current window
    pub remaining: Option<u32>,
    /// Reset time when the rate limit window resets
    pub reset_time: Option<DateTime<Utc>>,
    /// Limit of requests per window
    pub limit: Option<u32>,
}

impl RateLimitInfo {
    /// Check if rate limited (no remaining requests)
    pub fn is_rate_limited(&self) -> bool {
        matches!(self.remaining, Some(0))
    }

    /// Check if approaching rate limit (less than 10% remaining)
    pub fn is_approaching_limit(&self) -> bool {
        match (self.remaining, self.limit) {
            (Some(remaining), Some(limit)) => remaining < (limit / 10),
            _ => false,
        }
    }

    /// Get wait time until reset (in seconds)
    pub fn wait_seconds(&self) -> Option<u64> {
        self.reset_time.map(|reset| {
            let now = Utc::now();
            let duration = reset.signed_duration_since(now);
            duration.num_seconds().max(1) as u64
        })
    }
}

/// Parse GitHub rate limit headers
pub fn parse_github_rate_limits(headers: &HeaderMap) -> RateLimitInfo {
    RateLimitInfo {
        limit: headers
            .get("x-ratelimit-limit")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        remaining: headers
            .get("x-ratelimit-remaining")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        reset_time: headers
            .get("x-ratelimit-reset")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok())
            .map(|ts: u64| DateTime::from_timestamp(ts as i64, 0).unwrap_or_default()),
    }
}

/// Parse GitLab rate limit headers
pub fn parse_gitlab_rate_limits(headers: &HeaderMap) -> RateLimitInfo {
    RateLimitInfo {
        limit: headers
            .get("ratelimit-limit")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        remaining: headers
            .get("ratelimit-remaining")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        reset_time: headers
            .get("ratelimit-reset")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok())
            .map(|ts: u64| DateTime::from_timestamp(ts as i64, 0).unwrap_or_default()),
    }
}

/// Parse Azure DevOps rate limit headers
pub fn parse_azure_rate_limits(headers: &HeaderMap) -> RateLimitInfo {
    RateLimitInfo {
        limit: headers
            .get("x-ratelimit-limit")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        remaining: headers
            .get("x-ratelimit-remaining")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok()),
        reset_time: headers
            .get("x-ratelimit-reset")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok())
            .map(|ts: u64| DateTime::from_timestamp(ts as i64, 0).unwrap_or_default()),
    }
}

/// Display rate limit warning if approaching limit
pub fn check_rate_limit_warning(info: &RateLimitInfo, platform_name: &str) {
    if info.is_rate_limited() {
        if let Some(wait_seconds) = info.wait_seconds() {
            let wait_str = if wait_seconds < 60 {
                format!("{} seconds", wait_seconds)
            } else {
                format!("{} minutes", wait_seconds / 60)
            };
            Output::warning(&format!(
                "{} API rate limit reached. Waiting {} for reset...",
                platform_name, wait_str
            ));
        }
    } else if info.is_approaching_limit() {
        if let Some(remaining) = info.remaining {
            if let Some(limit) = info.limit {
                Output::info(&format!(
                    "{} API rate limit: {} of {} remaining",
                    platform_name, remaining, limit
                ));
            }
        }
    }
}

/// Sleep for rate limit wait time
pub async fn wait_for_rate_limit(info: &RateLimitInfo) -> Option<Duration> {
    if let Some(wait_seconds) = info.wait_seconds() {
        let duration = Duration::from_secs(wait_seconds);
        tokio::time::sleep(duration).await;
        return Some(duration);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::HeaderMap;

    // ── RateLimitInfo logic ─────────────────────────────────────

    #[test]
    fn test_is_rate_limited_when_zero_remaining() {
        let info = RateLimitInfo {
            remaining: Some(0),
            limit: Some(5000),
            reset_time: None,
        };
        assert!(info.is_rate_limited());
    }

    #[test]
    fn test_is_not_rate_limited_when_remaining() {
        let info = RateLimitInfo {
            remaining: Some(100),
            limit: Some(5000),
            reset_time: None,
        };
        assert!(!info.is_rate_limited());
    }

    #[test]
    fn test_is_not_rate_limited_when_none() {
        let info = RateLimitInfo {
            remaining: None,
            limit: None,
            reset_time: None,
        };
        assert!(!info.is_rate_limited());
    }

    #[test]
    fn test_is_approaching_limit() {
        // 45 remaining out of 500 = 9%, which is < 10%
        let info = RateLimitInfo {
            remaining: Some(45),
            limit: Some(500),
            reset_time: None,
        };
        assert!(info.is_approaching_limit());
    }

    #[test]
    fn test_is_not_approaching_limit() {
        // 100 remaining out of 500 = 20%, which is >= 10%
        let info = RateLimitInfo {
            remaining: Some(100),
            limit: Some(500),
            reset_time: None,
        };
        assert!(!info.is_approaching_limit());
    }

    #[test]
    fn test_is_approaching_limit_boundary() {
        // 50 remaining out of 500 = exactly 10% — limit/10 == 50, 50 < 50 is false
        let info = RateLimitInfo {
            remaining: Some(50),
            limit: Some(500),
            reset_time: None,
        };
        assert!(!info.is_approaching_limit());

        // 49 remaining out of 500 = 9.8%, which is < 10%
        let info = RateLimitInfo {
            remaining: Some(49),
            limit: Some(500),
            reset_time: None,
        };
        assert!(info.is_approaching_limit());
    }

    #[test]
    fn test_is_approaching_limit_none_fields() {
        let info = RateLimitInfo {
            remaining: None,
            limit: Some(500),
            reset_time: None,
        };
        assert!(!info.is_approaching_limit());

        let info = RateLimitInfo {
            remaining: Some(10),
            limit: None,
            reset_time: None,
        };
        assert!(!info.is_approaching_limit());
    }

    #[test]
    fn test_wait_seconds_future_reset() {
        let future = Utc::now() + chrono::Duration::seconds(120);
        let info = RateLimitInfo {
            remaining: Some(0),
            limit: Some(5000),
            reset_time: Some(future),
        };
        let wait = info.wait_seconds().unwrap();
        // Should be approximately 120 seconds (allow 2s tolerance for test execution)
        assert!((118..=122).contains(&wait), "wait_seconds was {}", wait);
    }

    #[test]
    fn test_wait_seconds_past_reset_clamps_to_1() {
        let past = Utc::now() - chrono::Duration::seconds(60);
        let info = RateLimitInfo {
            remaining: Some(0),
            limit: Some(5000),
            reset_time: Some(past),
        };
        let wait = info.wait_seconds().unwrap();
        assert_eq!(wait, 1, "past reset should clamp to 1 second");
    }

    #[test]
    fn test_wait_seconds_none_when_no_reset_time() {
        let info = RateLimitInfo {
            remaining: Some(0),
            limit: Some(5000),
            reset_time: None,
        };
        assert!(info.wait_seconds().is_none());
    }

    // ── Header parsing ──────────────────────────────────────────

    #[test]
    fn test_parse_github_rate_limits() {
        let mut headers = HeaderMap::new();
        headers.insert("x-ratelimit-limit", "5000".parse().unwrap());
        headers.insert("x-ratelimit-remaining", "4999".parse().unwrap());
        headers.insert("x-ratelimit-reset", "1700000000".parse().unwrap());

        let info = parse_github_rate_limits(&headers);
        assert_eq!(info.limit, Some(5000));
        assert_eq!(info.remaining, Some(4999));
        assert!(info.reset_time.is_some());
        assert!(!info.is_rate_limited());
        assert!(!info.is_approaching_limit());
    }

    #[test]
    fn test_parse_github_rate_limits_zero_remaining() {
        let mut headers = HeaderMap::new();
        headers.insert("x-ratelimit-limit", "5000".parse().unwrap());
        headers.insert("x-ratelimit-remaining", "0".parse().unwrap());
        headers.insert("x-ratelimit-reset", "1700000000".parse().unwrap());

        let info = parse_github_rate_limits(&headers);
        assert!(info.is_rate_limited());
    }

    #[test]
    fn test_parse_github_rate_limits_empty_headers() {
        let headers = HeaderMap::new();
        let info = parse_github_rate_limits(&headers);
        assert_eq!(info.limit, None);
        assert_eq!(info.remaining, None);
        assert!(info.reset_time.is_none());
        assert!(!info.is_rate_limited());
    }

    #[test]
    fn test_parse_gitlab_rate_limits() {
        let mut headers = HeaderMap::new();
        headers.insert("ratelimit-limit", "2000".parse().unwrap());
        headers.insert("ratelimit-remaining", "1500".parse().unwrap());
        headers.insert("ratelimit-reset", "1700000000".parse().unwrap());

        let info = parse_gitlab_rate_limits(&headers);
        assert_eq!(info.limit, Some(2000));
        assert_eq!(info.remaining, Some(1500));
        assert!(info.reset_time.is_some());
    }

    #[test]
    fn test_parse_gitlab_rate_limits_empty_headers() {
        let headers = HeaderMap::new();
        let info = parse_gitlab_rate_limits(&headers);
        assert_eq!(info.limit, None);
        assert_eq!(info.remaining, None);
        assert!(info.reset_time.is_none());
    }

    #[test]
    fn test_parse_azure_rate_limits() {
        let mut headers = HeaderMap::new();
        headers.insert("x-ratelimit-limit", "1000".parse().unwrap());
        headers.insert("x-ratelimit-remaining", "999".parse().unwrap());
        headers.insert("x-ratelimit-reset", "1700000000".parse().unwrap());

        let info = parse_azure_rate_limits(&headers);
        assert_eq!(info.limit, Some(1000));
        assert_eq!(info.remaining, Some(999));
        assert!(info.reset_time.is_some());
    }

    #[test]
    fn test_parse_azure_rate_limits_empty_headers() {
        let headers = HeaderMap::new();
        let info = parse_azure_rate_limits(&headers);
        assert_eq!(info.limit, None);
        assert_eq!(info.remaining, None);
        assert!(info.reset_time.is_none());
    }

    #[test]
    fn test_parse_invalid_header_values() {
        let mut headers = HeaderMap::new();
        headers.insert("x-ratelimit-limit", "not-a-number".parse().unwrap());
        headers.insert("x-ratelimit-remaining", "abc".parse().unwrap());
        headers.insert("x-ratelimit-reset", "xyz".parse().unwrap());

        let info = parse_github_rate_limits(&headers);
        assert_eq!(info.limit, None);
        assert_eq!(info.remaining, None);
        assert!(info.reset_time.is_none());
    }

    #[test]
    fn test_check_rate_limit_warning_no_panic() {
        // Rate limited
        let info = RateLimitInfo {
            remaining: Some(0),
            limit: Some(5000),
            reset_time: Some(Utc::now() + chrono::Duration::seconds(30)),
        };
        check_rate_limit_warning(&info, "GitHub");

        // Approaching limit
        let info = RateLimitInfo {
            remaining: Some(10),
            limit: Some(5000),
            reset_time: None,
        };
        check_rate_limit_warning(&info, "GitLab");

        // Normal
        let info = RateLimitInfo {
            remaining: Some(4000),
            limit: Some(5000),
            reset_time: None,
        };
        check_rate_limit_warning(&info, "Azure");
    }
}
