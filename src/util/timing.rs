//! Timing and benchmarking utilities
//!
//! Provides tools for measuring performance and comparing with TypeScript version.

use std::time::{Duration, Instant};

/// A timing entry for hierarchical measurements
#[derive(Debug, Clone)]
pub struct TimingEntry {
    /// Label for this timing
    pub label: String,
    /// Duration in milliseconds
    pub duration_ms: f64,
    /// Nested timings
    pub children: Vec<TimingEntry>,
}

impl TimingEntry {
    /// Create a new timing entry
    pub fn new(label: &str, duration: Duration) -> Self {
        Self {
            label: label.to_string(),
            duration_ms: duration.as_secs_f64() * 1000.0,
            children: Vec::new(),
        }
    }

    /// Add a child timing
    pub fn add_child(&mut self, child: TimingEntry) {
        self.children.push(child);
    }
}

/// A complete timing report
#[derive(Debug, Clone)]
pub struct TimingReport {
    /// Total duration in milliseconds
    pub total_ms: f64,
    /// Individual timing entries
    pub entries: Vec<TimingEntry>,
}

impl TimingReport {
    /// Create a new empty report
    pub fn new() -> Self {
        Self {
            total_ms: 0.0,
            entries: Vec::new(),
        }
    }

    /// Add a timing entry
    pub fn add_entry(&mut self, entry: TimingEntry) {
        self.total_ms += entry.duration_ms;
        self.entries.push(entry);
    }

    /// Print a formatted report
    pub fn print(&self) {
        println!("\n--- Timing Report ---");
        for entry in &self.entries {
            print_entry(entry, 0);
        }
        println!("---------------------");
        println!("Total: {:.2}ms", self.total_ms);
    }
}

fn print_entry(entry: &TimingEntry, depth: usize) {
    let indent = "  ".repeat(depth);
    println!("{}{}: {:.2}ms", indent, entry.label, entry.duration_ms);
    for child in &entry.children {
        print_entry(child, depth + 1);
    }
}

impl Default for TimingReport {
    fn default() -> Self {
        Self::new()
    }
}

/// A simple timer for measuring durations
#[derive(Debug)]
pub struct Timer {
    start: Instant,
    label: String,
    checkpoints: Vec<(String, Duration)>,
}

impl Timer {
    /// Create and start a new timer
    pub fn new(label: &str) -> Self {
        Self {
            start: Instant::now(),
            label: label.to_string(),
            checkpoints: Vec::new(),
        }
    }

    /// Start a new timer (alias for new)
    pub fn start(label: &str) -> Self {
        Self::new(label)
    }

    /// Get elapsed time without stopping
    pub fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }

    /// Get elapsed time in milliseconds
    pub fn elapsed_ms(&self) -> f64 {
        self.start.elapsed().as_secs_f64() * 1000.0
    }

    /// Record a checkpoint with the current elapsed time
    pub fn checkpoint(&mut self, label: &str) {
        self.checkpoints
            .push((label.to_string(), self.start.elapsed()));
    }

    /// Stop and return a timing entry
    pub fn stop(self) -> TimingEntry {
        let mut entry = TimingEntry::new(&self.label, self.start.elapsed());
        let mut prev_time = Duration::ZERO;
        for (label, time) in &self.checkpoints {
            let delta = *time - prev_time;
            entry.add_child(TimingEntry::new(label, delta));
            prev_time = *time;
        }
        entry
    }

    /// Stop and print the duration
    pub fn stop_and_print(self) {
        println!("{}: {:.2}ms", self.label, self.elapsed_ms());
        if !self.checkpoints.is_empty() {
            let mut prev_time = Duration::ZERO;
            for (label, time) in &self.checkpoints {
                let delta = *time - prev_time;
                let delta_ms = delta.as_secs_f64() * 1000.0;
                println!("  {}: {:.2}ms", label, delta_ms);
                prev_time = *time;
            }
        }
    }
}

/// Benchmark result for a single operation
#[derive(Debug, Clone)]
pub struct BenchmarkResult {
    /// Benchmark name
    pub name: String,
    /// Number of iterations
    pub iterations: u32,
    /// Minimum duration in milliseconds
    pub min_ms: f64,
    /// Maximum duration in milliseconds
    pub max_ms: f64,
    /// Average duration in milliseconds
    pub avg_ms: f64,
    /// Median (50th percentile)
    pub p50_ms: f64,
    /// 95th percentile
    pub p95_ms: f64,
    /// Standard deviation
    pub std_dev_ms: f64,
}

impl BenchmarkResult {
    /// Print a formatted benchmark result
    pub fn print(&self) {
        println!("\n--- Benchmark: {} ---", self.name);
        println!("Iterations: {}", self.iterations);
        println!("Min:    {:.3}ms", self.min_ms);
        println!("Max:    {:.3}ms", self.max_ms);
        println!("Avg:    {:.3}ms", self.avg_ms);
        println!("P50:    {:.3}ms", self.p50_ms);
        println!("P95:    {:.3}ms", self.p95_ms);
        println!("StdDev: {:.3}ms", self.std_dev_ms);
    }

    /// Format as a comparison-friendly string
    pub fn to_comparison_string(&self) -> String {
        format!(
            "{}: avg={:.3}ms, p50={:.3}ms, p95={:.3}ms (n={})",
            self.name, self.avg_ms, self.p50_ms, self.p95_ms, self.iterations
        )
    }
}

/// Run a benchmark
pub fn benchmark<F>(name: &str, iterations: u32, mut f: F) -> BenchmarkResult
where
    F: FnMut(),
{
    let mut durations: Vec<f64> = Vec::with_capacity(iterations as usize);

    // Warmup
    for _ in 0..3 {
        f();
    }

    // Actual benchmark
    for _ in 0..iterations {
        let start = Instant::now();
        f();
        durations.push(start.elapsed().as_secs_f64() * 1000.0);
    }

    // Sort for percentile calculations
    durations.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let min = durations[0];
    let max = durations[durations.len() - 1];
    let sum: f64 = durations.iter().sum();
    let avg = sum / iterations as f64;

    let p50_idx = (iterations as f64 * 0.50) as usize;
    let p95_idx = (iterations as f64 * 0.95) as usize;
    let p50 = durations[p50_idx.min(durations.len() - 1)];
    let p95 = durations[p95_idx.min(durations.len() - 1)];

    // Standard deviation
    let variance: f64 =
        durations.iter().map(|d| (d - avg).powi(2)).sum::<f64>() / iterations as f64;
    let std_dev = variance.sqrt();

    BenchmarkResult {
        name: name.to_string(),
        iterations,
        min_ms: min,
        max_ms: max,
        avg_ms: avg,
        p50_ms: p50,
        p95_ms: p95,
        std_dev_ms: std_dev,
    }
}

/// Run an async benchmark
pub async fn benchmark_async<F, Fut>(name: &str, iterations: u32, mut f: F) -> BenchmarkResult
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    let mut durations: Vec<f64> = Vec::with_capacity(iterations as usize);

    // Warmup
    for _ in 0..3 {
        f().await;
    }

    // Actual benchmark
    for _ in 0..iterations {
        let start = Instant::now();
        f().await;
        durations.push(start.elapsed().as_secs_f64() * 1000.0);
    }

    // Sort for percentile calculations
    durations.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let min = durations[0];
    let max = durations[durations.len() - 1];
    let sum: f64 = durations.iter().sum();
    let avg = sum / iterations as f64;

    let p50_idx = (iterations as f64 * 0.50) as usize;
    let p95_idx = (iterations as f64 * 0.95) as usize;
    let p50 = durations[p50_idx.min(durations.len() - 1)];
    let p95 = durations[p95_idx.min(durations.len() - 1)];

    let variance: f64 =
        durations.iter().map(|d| (d - avg).powi(2)).sum::<f64>() / iterations as f64;
    let std_dev = variance.sqrt();

    BenchmarkResult {
        name: name.to_string(),
        iterations,
        min_ms: min,
        max_ms: max,
        avg_ms: avg,
        p50_ms: p50,
        p95_ms: p95,
        std_dev_ms: std_dev,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn test_timer() {
        let timer = Timer::start("test");
        thread::sleep(Duration::from_millis(10));
        let entry = timer.stop();
        assert!(entry.duration_ms >= 10.0);
    }

    #[test]
    fn test_benchmark() {
        let result = benchmark("sleep_1ms", 10, || {
            thread::sleep(Duration::from_millis(1));
        });
        assert_eq!(result.iterations, 10);
        assert!(result.avg_ms >= 1.0);
        assert!(result.min_ms <= result.max_ms);
    }

    #[test]
    fn test_timing_report() {
        let mut report = TimingReport::new();
        report.add_entry(TimingEntry::new("op1", Duration::from_millis(10)));
        report.add_entry(TimingEntry::new("op2", Duration::from_millis(20)));
        assert!(report.total_ms >= 30.0);
    }

    #[test]
    fn test_timer_checkpoint() {
        let mut timer = Timer::start("with-checkpoints");
        thread::sleep(Duration::from_millis(10));
        timer.checkpoint("step1");
        thread::sleep(Duration::from_millis(10));
        timer.checkpoint("step2");
        let entry = timer.stop();
        assert_eq!(entry.children.len(), 2);
        assert_eq!(entry.children[0].label, "step1");
        assert_eq!(entry.children[1].label, "step2");
        assert!(entry.children[0].duration_ms >= 5.0);
        assert!(entry.children[1].duration_ms >= 5.0);
    }

    #[test]
    fn test_timing_entry_children() {
        let mut parent = TimingEntry::new("parent", Duration::from_millis(100));
        parent.add_child(TimingEntry::new("child1", Duration::from_millis(40)));
        parent.add_child(TimingEntry::new("child2", Duration::from_millis(60)));
        assert_eq!(parent.children.len(), 2);
        assert_eq!(parent.children[0].label, "child1");
        assert_eq!(parent.children[1].label, "child2");
    }

    #[test]
    fn test_timing_report_default() {
        let report = TimingReport::default();
        assert_eq!(report.total_ms, 0.0);
        assert!(report.entries.is_empty());
    }

    #[test]
    fn test_timer_elapsed() {
        let timer = Timer::new("elapsed-test");
        thread::sleep(Duration::from_millis(10));
        let elapsed = timer.elapsed();
        assert!(elapsed >= Duration::from_millis(5));
        let ms = timer.elapsed_ms();
        assert!(ms >= 5.0);
    }

    #[test]
    fn test_benchmark_result_comparison_string() {
        let result = BenchmarkResult {
            name: "test-op".to_string(),
            iterations: 100,
            min_ms: 1.0,
            max_ms: 5.0,
            avg_ms: 2.5,
            p50_ms: 2.0,
            p95_ms: 4.5,
            std_dev_ms: 1.0,
        };
        let s = result.to_comparison_string();
        assert!(s.contains("test-op"));
        assert!(s.contains("avg=2.500ms"));
        assert!(s.contains("p50=2.000ms"));
        assert!(s.contains("p95=4.500ms"));
        assert!(s.contains("n=100"));
    }

    #[test]
    fn test_benchmark_statistics() {
        let result = benchmark("counter", 20, || {
            // Just a no-op for fast iteration
            let _ = 1 + 1;
        });
        assert_eq!(result.iterations, 20);
        assert!(result.min_ms <= result.avg_ms);
        assert!(result.avg_ms <= result.max_ms);
        assert!(result.p50_ms <= result.p95_ms);
        assert!(result.std_dev_ms >= 0.0);
    }

    #[tokio::test]
    async fn test_benchmark_async() {
        let result = benchmark_async("async-op", 10, || async {
            tokio::time::sleep(Duration::from_millis(1)).await;
        })
        .await;
        assert_eq!(result.iterations, 10);
        assert!(result.avg_ms >= 0.5);
        assert!(result.min_ms <= result.max_ms);
    }
}
