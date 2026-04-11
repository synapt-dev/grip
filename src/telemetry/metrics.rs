//! Metrics collection for git and platform operations.
//!
//! Provides in-memory metrics tracking with histograms for latency distribution.

use once_cell::sync::Lazy;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

/// Global metrics instance.
pub static GLOBAL_METRICS: Lazy<Metrics> = Lazy::new(Metrics::new);

/// Centralized metrics collection.
pub struct Metrics {
    git_metrics: Mutex<HashMap<String, GitMetrics>>,
    platform_metrics: Mutex<HashMap<String, PlatformMetrics>>,
    operation_metrics: Mutex<HashMap<String, OperationMetrics>>,
}

impl Metrics {
    /// Create a new metrics instance.
    pub fn new() -> Self {
        Self {
            git_metrics: Mutex::new(HashMap::new()),
            platform_metrics: Mutex::new(HashMap::new()),
            operation_metrics: Mutex::new(HashMap::new()),
        }
    }

    /// Record a git operation.
    pub fn record_git(&self, operation: &str, duration: Duration, success: bool) {
        let mut metrics = self.git_metrics.lock().expect("mutex poisoned");
        let entry = metrics.entry(operation.to_string()).or_default();
        entry.record(duration, success);
    }

    /// Record a platform API call.
    pub fn record_platform(
        &self,
        platform: &str,
        operation: &str,
        duration: Duration,
        success: bool,
    ) {
        let key = format!("{platform}:{operation}");
        let mut metrics = self.platform_metrics.lock().expect("mutex poisoned");
        let entry = metrics.entry(key).or_default();
        entry.record(duration, success);
    }

    /// Record a generic operation.
    pub fn record_operation(&self, name: &str, duration: Duration) {
        let mut metrics = self.operation_metrics.lock().expect("mutex poisoned");
        let entry = metrics.entry(name.to_string()).or_default();
        entry.record(duration);
    }

    /// Record a cache hit/miss for git status cache.
    pub fn record_cache(&self, hit: bool) {
        let name = if hit { "cache_hit" } else { "cache_miss" };
        self.record_operation(name, Duration::ZERO);
    }

    /// Get a snapshot of all metrics.
    pub fn snapshot(&self) -> MetricsSnapshot {
        let git = self.git_metrics.lock().expect("mutex poisoned").clone();
        let platform = self
            .platform_metrics
            .lock()
            .expect("mutex poisoned")
            .clone();
        let operations = self
            .operation_metrics
            .lock()
            .expect("mutex poisoned")
            .clone();

        MetricsSnapshot {
            git,
            platform,
            operations,
        }
    }

    /// Reset all metrics.
    pub fn reset(&self) {
        self.git_metrics.lock().expect("mutex poisoned").clear();
        self.platform_metrics
            .lock()
            .expect("mutex poisoned")
            .clear();
        self.operation_metrics
            .lock()
            .expect("mutex poisoned")
            .clear();
    }
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

/// Metrics for git operations.
#[derive(Debug, Clone)]
pub struct GitMetrics {
    pub invocations: u64,
    pub successes: u64,
    pub failures: u64,
    pub total_duration: Duration,
    pub min_duration: Duration,
    pub max_duration: Duration,
    pub histogram: Histogram,
}

impl GitMetrics {
    /// Create new git metrics.
    pub fn new() -> Self {
        Self {
            invocations: 0,
            successes: 0,
            failures: 0,
            total_duration: Duration::ZERO,
            min_duration: Duration::MAX,
            max_duration: Duration::ZERO,
            histogram: Histogram::new(),
        }
    }

    /// Record a git operation.
    pub fn record(&mut self, duration: Duration, success: bool) {
        self.invocations += 1;
        if success {
            self.successes += 1;
        } else {
            self.failures += 1;
        }
        self.total_duration += duration;
        self.min_duration = self.min_duration.min(duration);
        self.max_duration = self.max_duration.max(duration);
        self.histogram.record(duration);
    }

    /// Get average duration.
    pub fn avg_duration(&self) -> Duration {
        if self.invocations == 0 {
            Duration::ZERO
        } else {
            self.total_duration / self.invocations as u32
        }
    }

    /// Get success rate as a percentage.
    pub fn success_rate(&self) -> f64 {
        if self.invocations == 0 {
            100.0
        } else {
            (self.successes as f64 / self.invocations as f64) * 100.0
        }
    }
}

impl Default for GitMetrics {
    fn default() -> Self {
        Self::new()
    }
}

/// Metrics for platform API calls.
#[derive(Debug, Clone)]
pub struct PlatformMetrics {
    pub invocations: u64,
    pub successes: u64,
    pub failures: u64,
    pub rate_limits: u64,
    pub total_duration: Duration,
    pub min_duration: Duration,
    pub max_duration: Duration,
    pub histogram: Histogram,
}

impl PlatformMetrics {
    /// Create new platform metrics.
    pub fn new() -> Self {
        Self {
            invocations: 0,
            successes: 0,
            failures: 0,
            rate_limits: 0,
            total_duration: Duration::ZERO,
            min_duration: Duration::MAX,
            max_duration: Duration::ZERO,
            histogram: Histogram::new(),
        }
    }

    /// Record a platform API call.
    pub fn record(&mut self, duration: Duration, success: bool) {
        self.invocations += 1;
        if success {
            self.successes += 1;
        } else {
            self.failures += 1;
        }
        self.total_duration += duration;
        self.min_duration = self.min_duration.min(duration);
        self.max_duration = self.max_duration.max(duration);
        self.histogram.record(duration);
    }

    /// Record a rate limit hit.
    pub fn record_rate_limit(&mut self) {
        self.rate_limits += 1;
    }

    /// Get average duration.
    pub fn avg_duration(&self) -> Duration {
        if self.invocations == 0 {
            Duration::ZERO
        } else {
            self.total_duration / self.invocations as u32
        }
    }
}

impl Default for PlatformMetrics {
    fn default() -> Self {
        Self::new()
    }
}

/// Metrics for generic operations.
#[derive(Debug, Clone)]
pub struct OperationMetrics {
    pub count: u64,
    pub total_duration: Duration,
    pub histogram: Histogram,
}

impl OperationMetrics {
    /// Create new operation metrics.
    pub fn new() -> Self {
        Self {
            count: 0,
            total_duration: Duration::ZERO,
            histogram: Histogram::new(),
        }
    }

    /// Record an operation.
    pub fn record(&mut self, duration: Duration) {
        self.count += 1;
        self.total_duration += duration;
        self.histogram.record(duration);
    }
}

impl Default for OperationMetrics {
    fn default() -> Self {
        Self::new()
    }
}

/// Simple histogram for latency distribution.
#[derive(Debug, Clone, Default)]
pub struct Histogram {
    samples: Vec<Duration>,
}

impl Histogram {
    /// Create a new histogram.
    pub fn new() -> Self {
        Self {
            samples: Vec::new(),
        }
    }

    /// Record a sample.
    pub fn record(&mut self, duration: Duration) {
        self.samples.push(duration);
    }

    /// Get the number of samples.
    pub fn count(&self) -> usize {
        self.samples.len()
    }

    /// Get the 50th percentile (median).
    pub fn p50(&self) -> Option<Duration> {
        self.percentile(50)
    }

    /// Get the 90th percentile.
    pub fn p90(&self) -> Option<Duration> {
        self.percentile(90)
    }

    /// Get the 95th percentile.
    pub fn p95(&self) -> Option<Duration> {
        self.percentile(95)
    }

    /// Get the 99th percentile.
    pub fn p99(&self) -> Option<Duration> {
        self.percentile(99)
    }

    /// Get the specified percentile.
    pub fn percentile(&self, p: u8) -> Option<Duration> {
        if self.samples.is_empty() {
            return None;
        }

        let mut sorted = self.samples.clone();
        sorted.sort();

        let index = (p as f64 / 100.0 * (sorted.len() - 1) as f64).round() as usize;
        Some(sorted[index.min(sorted.len() - 1)])
    }
}

/// Snapshot of all metrics at a point in time.
#[derive(Debug, Clone)]
pub struct MetricsSnapshot {
    pub git: HashMap<String, GitMetrics>,
    pub platform: HashMap<String, PlatformMetrics>,
    pub operations: HashMap<String, OperationMetrics>,
}

impl MetricsSnapshot {
    /// Format as a human-readable report.
    pub fn format_report(&self) -> String {
        let mut report = String::new();
        report.push_str("=== Metrics Report ===\n\n");

        if !self.git.is_empty() {
            report.push_str("Git Operations:\n");
            for (name, metrics) in &self.git {
                report.push_str(&format!(
                    "  {}: {} calls, {:.1}% success, avg {:.2}ms\n",
                    name,
                    metrics.invocations,
                    metrics.success_rate(),
                    metrics.avg_duration().as_secs_f64() * 1000.0
                ));
            }
            report.push('\n');
        }

        if !self.platform.is_empty() {
            report.push_str("Platform API Calls:\n");
            for (name, metrics) in &self.platform {
                report.push_str(&format!(
                    "  {}: {} calls, {} rate limits, avg {:.2}ms\n",
                    name,
                    metrics.invocations,
                    metrics.rate_limits,
                    metrics.avg_duration().as_secs_f64() * 1000.0
                ));
            }
            report.push('\n');
        }

        if !self.operations.is_empty() {
            report.push_str("Other Operations:\n");
            for (name, metrics) in &self.operations {
                report.push_str(&format!("  {}: {} calls\n", name, metrics.count));
            }
        }

        report
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_git_metrics() {
        let mut metrics = GitMetrics::new();
        metrics.record(Duration::from_millis(100), true);
        metrics.record(Duration::from_millis(200), true);
        metrics.record(Duration::from_millis(150), false);

        assert_eq!(metrics.invocations, 3);
        assert_eq!(metrics.successes, 2);
        assert_eq!(metrics.failures, 1);
        assert!(metrics.success_rate() > 66.0 && metrics.success_rate() < 67.0);
    }

    #[test]
    fn test_histogram_percentiles() {
        let mut hist = Histogram::new();
        for i in 1..=100 {
            hist.record(Duration::from_millis(i));
        }

        // p50 should be around 50 (rounding may cause slight differences)
        let p50 = hist.p50().unwrap().as_millis();
        assert!((49..=51).contains(&p50), "p50 was {p50}, expected ~50");
        assert!(hist.p99().unwrap() >= Duration::from_millis(99));
    }

    #[test]
    fn test_global_metrics() {
        GLOBAL_METRICS.record_git("clone", Duration::from_millis(1000), true);
        GLOBAL_METRICS.record_platform("github", "create_pr", Duration::from_millis(500), true);
        GLOBAL_METRICS.record_cache(true);
        GLOBAL_METRICS.record_cache(false);

        let snapshot = GLOBAL_METRICS.snapshot();
        assert!(snapshot.git.contains_key("clone"));
        assert!(snapshot.platform.contains_key("github:create_pr"));
    }
}
