//! Retry logic with exponential backoff

use std::future::Future;
use std::time::Duration;
use tokio::time::sleep;

/// Options for retry behavior
#[derive(Debug, Clone)]
pub struct RetryOptions {
    /// Maximum number of retries (default: 3)
    pub max_retries: u32,
    /// Initial delay between retries in milliseconds (default: 1000)
    pub initial_delay_ms: u64,
    /// Maximum delay between retries in milliseconds (default: 30000)
    pub max_delay_ms: u64,
    /// Jitter factor (0.0-1.0) to randomize delays (default: 0.1)
    pub jitter: f64,
}

impl Default for RetryOptions {
    fn default() -> Self {
        Self {
            max_retries: 3,
            initial_delay_ms: 1000,
            max_delay_ms: 30000,
            jitter: 0.1,
        }
    }
}

impl RetryOptions {
    /// Calculate delay for a given attempt with exponential backoff
    pub fn calculate_delay(&self, attempt: u32) -> Duration {
        // Base delay: initial * 2^attempt
        let base_delay = self.initial_delay_ms as f64 * 2.0_f64.powi(attempt as i32);

        // Clamp to max delay
        let clamped = base_delay.min(self.max_delay_ms as f64);

        // Add jitter
        let jitter_amount = clamped * self.jitter * rand_float();
        let final_delay = clamped + jitter_amount;

        Duration::from_millis(final_delay as u64)
    }
}

/// Simple pseudo-random float between 0 and 1
fn rand_float() -> f64 {
    use std::time::SystemTime;
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .subsec_nanos();
    (nanos % 1000) as f64 / 1000.0
}

/// Error types that are retryable
pub fn is_retryable_error(error: &str) -> bool {
    let retryable_patterns = [
        "ECONNRESET",
        "ECONNREFUSED",
        "ETIMEDOUT",
        "ENOTFOUND",
        "socket hang up",
        "connection reset",
        "timeout",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
    ];

    let error_lower = error.to_lowercase();
    retryable_patterns
        .iter()
        .any(|p| error_lower.contains(&p.to_lowercase()))
}

/// Retry an async operation with exponential backoff
pub async fn retry_with_backoff<T, E, F, Fut>(
    options: &RetryOptions,
    mut operation: F,
) -> Result<T, E>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, E>>,
    E: std::fmt::Display,
{
    let mut attempt = 0;

    loop {
        match operation().await {
            Ok(result) => return Ok(result),
            Err(error) => {
                if attempt >= options.max_retries {
                    return Err(error);
                }

                // Check if error is retryable
                let error_str = error.to_string();
                if !is_retryable_error(&error_str) {
                    return Err(error);
                }

                let delay = options.calculate_delay(attempt);
                tracing::warn!(
                    "Attempt {} failed: {}. Retrying in {:?}",
                    attempt + 1,
                    error_str,
                    delay
                );

                sleep(delay).await;
                attempt += 1;
            }
        }
    }
}

/// Retry callback for custom handling
pub type OnRetryFn = Box<dyn Fn(u32, &str, Duration) + Send + Sync>;

/// Extended retry with custom callback
pub async fn retry_with_callback<T, E, F, Fut>(
    options: &RetryOptions,
    mut operation: F,
    on_retry: Option<OnRetryFn>,
) -> Result<T, E>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, E>>,
    E: std::fmt::Display,
{
    let mut attempt = 0;

    loop {
        match operation().await {
            Ok(result) => return Ok(result),
            Err(error) => {
                if attempt >= options.max_retries {
                    return Err(error);
                }

                let error_str = error.to_string();
                if !is_retryable_error(&error_str) {
                    return Err(error);
                }

                let delay = options.calculate_delay(attempt);

                if let Some(ref callback) = on_retry {
                    callback(attempt + 1, &error_str, delay);
                }

                sleep(delay).await;
                attempt += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_calculate_delay() {
        let options = RetryOptions {
            initial_delay_ms: 1000,
            max_delay_ms: 30000,
            jitter: 0.0, // No jitter for predictable testing
            ..Default::default()
        };

        // First attempt: 1000ms
        let delay0 = options.calculate_delay(0);
        assert_eq!(delay0.as_millis(), 1000);

        // Second attempt: 2000ms
        let delay1 = options.calculate_delay(1);
        assert_eq!(delay1.as_millis(), 2000);

        // Third attempt: 4000ms
        let delay2 = options.calculate_delay(2);
        assert_eq!(delay2.as_millis(), 4000);
    }

    #[test]
    fn test_max_delay_clamping() {
        let options = RetryOptions {
            initial_delay_ms: 1000,
            max_delay_ms: 5000,
            jitter: 0.0,
            ..Default::default()
        };

        // 1000 * 2^10 = 1024000, should clamp to 5000
        let delay = options.calculate_delay(10);
        assert_eq!(delay.as_millis(), 5000);
    }

    #[test]
    fn test_is_retryable_error() {
        assert!(is_retryable_error("ECONNRESET"));
        assert!(is_retryable_error("connection timeout"));
        assert!(is_retryable_error("HTTP 429 Too Many Requests"));
        assert!(is_retryable_error("HTTP 503 Service Unavailable"));
        assert!(!is_retryable_error("Not found"));
        assert!(!is_retryable_error("HTTP 404"));
        assert!(!is_retryable_error("Invalid token"));
    }

    #[tokio::test]
    async fn test_retry_success_first_try() {
        let options = RetryOptions::default();
        let result: Result<i32, &str> = retry_with_backoff(&options, || async { Ok(42) }).await;
        assert_eq!(result.unwrap(), 42);
    }

    #[tokio::test]
    async fn test_retry_non_retryable_error() {
        let options = RetryOptions {
            max_retries: 3,
            initial_delay_ms: 10,
            ..Default::default()
        };

        let mut attempts = 0;
        let result: Result<i32, String> = retry_with_backoff(&options, || {
            attempts += 1;
            async { Err("Not found".to_string()) }
        })
        .await;

        assert!(result.is_err());
        assert_eq!(attempts, 1); // Should not retry non-retryable errors
    }

    #[test]
    fn test_calculate_delay_with_jitter() {
        let options = RetryOptions {
            initial_delay_ms: 1000,
            max_delay_ms: 30000,
            jitter: 0.5,
            ..Default::default()
        };
        let delay = options.calculate_delay(0);
        // With jitter 0.5, delay should be between 1000 and 1500ms
        assert!(delay.as_millis() >= 1000);
        assert!(delay.as_millis() <= 1500);
    }

    #[test]
    fn test_default_retry_options() {
        let opts = RetryOptions::default();
        assert_eq!(opts.max_retries, 3);
        assert_eq!(opts.initial_delay_ms, 1000);
        assert_eq!(opts.max_delay_ms, 30000);
        assert!((opts.jitter - 0.1).abs() < f64::EPSILON);
    }

    #[test]
    fn test_is_retryable_error_case_insensitive() {
        assert!(is_retryable_error("Connection Reset by peer"));
        assert!(is_retryable_error("TIMEOUT waiting for response"));
        assert!(is_retryable_error("Rate Limit exceeded"));
    }

    #[test]
    fn test_is_retryable_error_http_codes() {
        assert!(is_retryable_error("HTTP 500 Internal Server Error"));
        assert!(is_retryable_error("HTTP 502 Bad Gateway"));
        assert!(is_retryable_error("HTTP 504 Gateway Timeout"));
        assert!(!is_retryable_error("HTTP 401 Unauthorized"));
        assert!(!is_retryable_error("HTTP 403 Forbidden"));
    }

    #[tokio::test]
    async fn test_retry_with_callback_success() {
        let options = RetryOptions::default();
        let result: Result<i32, String> =
            retry_with_callback(&options, || async { Ok(42) }, None).await;
        assert_eq!(result.unwrap(), 42);
    }

    #[tokio::test]
    async fn test_retry_with_callback_invoked() {
        use std::sync::{Arc, Mutex};

        let options = RetryOptions {
            max_retries: 2,
            initial_delay_ms: 1,
            max_delay_ms: 10,
            jitter: 0.0,
        };

        let callback_calls = Arc::new(Mutex::new(Vec::new()));
        let cb_clone = callback_calls.clone();
        let on_retry: OnRetryFn = Box::new(move |attempt, error, _delay| {
            cb_clone.lock().unwrap().push((attempt, error.to_string()));
        });

        let mut attempt = 0;
        let _result: Result<i32, String> = retry_with_callback(
            &options,
            || {
                attempt += 1;
                async move { Err("connection timeout".to_string()) }
            },
            Some(on_retry),
        )
        .await;

        let calls = callback_calls.lock().unwrap();
        assert_eq!(calls.len(), 2); // Called on retry 1 and 2
        assert_eq!(calls[0].0, 1);
        assert_eq!(calls[1].0, 2);
    }

    #[tokio::test]
    async fn test_retry_exhausts_max_retries() {
        let options = RetryOptions {
            max_retries: 2,
            initial_delay_ms: 1,
            max_delay_ms: 10,
            jitter: 0.0,
        };

        let mut attempts = 0;
        let result: Result<i32, String> = retry_with_backoff(&options, || {
            attempts += 1;
            async { Err("ECONNRESET".to_string()) }
        })
        .await;

        assert!(result.is_err());
        assert_eq!(attempts, 3); // 1 initial + 2 retries
    }
}
