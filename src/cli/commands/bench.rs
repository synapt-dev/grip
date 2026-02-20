//! Benchmark core operations

use std::time::Instant;

use anyhow::Result;
use clap::Args;
use colored::Colorize;

use crate::core::manifest::{Manifest, ManifestSettings, RepoConfig};
use crate::core::repo::RepoInfo;
use crate::core::state::StateFile;

#[derive(Args, Debug)]
pub struct BenchArgs {
    /// Specific benchmark to run (omit to run all)
    #[arg()]
    pub operation: Option<String>,

    /// List available benchmarks
    #[arg(short, long)]
    pub list: bool,

    /// Number of iterations
    #[arg(short = 'n', long, default_value = "10")]
    pub iterations: usize,

    /// Number of warmup iterations
    #[arg(short, long, default_value = "2")]
    pub warmup: usize,

    /// Output as JSON
    #[arg(long)]
    pub json: bool,
}

/// Benchmark result
#[derive(Debug, serde::Serialize)]
pub struct BenchmarkResult {
    pub name: String,
    pub iterations: usize,
    pub min: f64,
    pub max: f64,
    pub avg: f64,
    pub p50: f64,
    pub p95: f64,
    pub std_dev: f64,
}

/// Available benchmarks
struct Benchmark {
    name: &'static str,
    description: &'static str,
}

const BENCHMARKS: &[Benchmark] = &[
    Benchmark {
        name: "manifest-parse",
        description: "Parse manifest YAML",
    },
    Benchmark {
        name: "state-parse",
        description: "Parse state JSON file",
    },
    Benchmark {
        name: "url-parse",
        description: "Parse git URL to RepoInfo",
    },
];

/// Sample manifest YAML for benchmarking
const SAMPLE_MANIFEST: &str = r#"
version: 1
repos:
  app:
    url: git@github.com:user/app.git
    path: app
    default_branch: main
  lib:
    url: git@github.com:user/lib.git
    path: lib
  common:
    url: git@github.com:user/common.git
    path: common
    default_branch: develop
settings:
  pr_prefix: "[multi-repo]"
  merge_strategy: all-or-nothing
"#;

/// Sample state JSON for benchmarking
const SAMPLE_STATE: &str = r#"{
    "currentManifestPr": 42,
    "branchToPr": {
        "feat/test": 42,
        "feat/another": 43,
        "fix/bug": 44
    },
    "prLinks": {
        "42": [
            {
                "repoName": "app",
                "owner": "user",
                "repo": "app",
                "number": 100,
                "url": "https://github.com/user/app/pull/100",
                "state": "open",
                "approved": true,
                "checksPass": true,
                "mergeable": true
            },
            {
                "repoName": "lib",
                "owner": "user",
                "repo": "lib",
                "number": 101,
                "url": "https://github.com/user/lib/pull/101",
                "state": "open",
                "approved": false,
                "checksPass": true,
                "mergeable": true
            }
        ]
    }
}"#;

/// List available benchmarks
fn list_benchmarks() {
    println!("{}\n", "Available Benchmarks".blue());

    let max_name_len = BENCHMARKS.iter().map(|b| b.name.len()).max().unwrap_or(0);

    for bench in BENCHMARKS {
        println!(
            "  {}{}",
            bench.name.cyan(),
            " ".repeat(max_name_len - bench.name.len() + 2),
        );
        println!("      {}", bench.description);
    }

    println!();
    println!("{}", "Run a specific benchmark:".dimmed());
    println!("{}", "  gr bench manifest-parse".dimmed());
    println!("{}", "  gr bench state-parse -n 100".dimmed());
    println!();
    println!("{}", "Run all benchmarks:".dimmed());
    println!("{}", "  gr bench".dimmed());
}

/// Calculate statistics from durations
fn calculate_stats(durations: &[f64]) -> (f64, f64, f64, f64, f64, f64) {
    if durations.is_empty() {
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0);
    }

    let mut sorted = durations.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let n = sorted.len();
    let min = sorted[0];
    let max = sorted[n - 1];
    let sum: f64 = sorted.iter().sum();
    let avg = sum / n as f64;

    // Percentiles
    let p50_idx = (n as f64 * 0.5).floor() as usize;
    let p95_idx = (n as f64 * 0.95).floor() as usize;
    let p50 = sorted[p50_idx.min(n - 1)];
    let p95 = sorted[p95_idx.min(n - 1)];

    // Standard deviation
    let variance: f64 = sorted.iter().map(|x| (x - avg).powi(2)).sum::<f64>() / n as f64;
    let std_dev = variance.sqrt();

    (min, max, avg, p50, p95, std_dev)
}

/// Format duration in milliseconds
fn format_duration(ms: f64) -> String {
    if ms < 0.001 {
        format!("{:.0}ns", ms * 1_000_000.0)
    } else if ms < 1.0 {
        format!("{:.0}µs", ms * 1000.0)
    } else if ms < 1000.0 {
        format!("{:.2}ms", ms)
    } else if ms < 60000.0 {
        format!("{:.2}s", ms / 1000.0)
    } else {
        let minutes = (ms / 60000.0).floor();
        let seconds = (ms % 60000.0) / 1000.0;
        format!("{:.0}m {:.1}s", minutes, seconds)
    }
}

/// Run a benchmark operation once
fn run_benchmark_operation(name: &str) -> Result<()> {
    match name {
        "manifest-parse" => {
            let _ = Manifest::parse(SAMPLE_MANIFEST)?;
            Ok(())
        }
        "state-parse" => {
            let _ = StateFile::parse(SAMPLE_STATE)?;
            Ok(())
        }
        "url-parse" => {
            let config = RepoConfig {
                url: Some("git@github.com:user/repo.git".to_string()),
                remote: None,
                path: "repo".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: Vec::new(),
                agent: None,
            };
            let workspace = std::path::PathBuf::from("/workspace");
            let _ = RepoInfo::from_config(
                "repo",
                &config,
                &workspace,
                &ManifestSettings::default(),
                None,
            );
            Ok(())
        }
        _ => Err(anyhow::anyhow!(
            "Unknown benchmark: {}. Use --list to see available benchmarks.",
            name
        )),
    }
}

/// Run a single benchmark with warmup and iterations
fn run_single_benchmark(name: &str, iterations: usize, warmup: usize) -> Result<BenchmarkResult> {
    let mut durations = Vec::with_capacity(iterations);

    // Warmup runs
    for _ in 0..warmup {
        run_benchmark_operation(name)?;
    }

    // Actual benchmark runs
    for _ in 0..iterations {
        let start = Instant::now();
        run_benchmark_operation(name)?;
        let elapsed = start.elapsed();
        durations.push(elapsed.as_secs_f64() * 1000.0); // Convert to ms
    }

    let (min, max, avg, p50, p95, std_dev) = calculate_stats(&durations);

    Ok(BenchmarkResult {
        name: name.to_string(),
        iterations,
        min,
        max,
        avg,
        p50,
        p95,
        std_dev,
    })
}

/// Format benchmark results as a table
fn format_results(results: &[BenchmarkResult]) -> String {
    let mut lines = vec![
        "Benchmark Results".to_string(),
        "═════════════════".to_string(),
        String::new(),
        // Header
        "Operation        │ Iter │      Min │      Max │      Avg │      P95".to_string(),
        "─────────────────┼──────┼──────────┼──────────┼──────────┼──────────".to_string(),
    ];

    // Rows
    for result in results {
        let name = format!("{:16}", result.name);
        let iter = format!("{:4}", result.iterations);
        let min = format!("{:>8}", format_duration(result.min));
        let max = format!("{:>8}", format_duration(result.max));
        let avg = format!("{:>8}", format_duration(result.avg));
        let p95 = format!("{:>8}", format_duration(result.p95));
        lines.push(format!(
            "{} │ {} │ {} │ {} │ {} │ {}",
            name, iter, min, max, avg, p95
        ));
    }

    lines.join("\n")
}

/// Benchmark core operations
pub async fn run(args: BenchArgs) -> Result<()> {
    // List mode
    if args.list {
        list_benchmarks();
        return Ok(());
    }

    let mut results = Vec::new();

    if let Some(ref operation) = args.operation {
        // Run single benchmark
        if !args.json {
            println!("{}\n", format!("Running benchmark: {}", operation).blue());
            println!(
                "{}\n",
                format!("Iterations: {}, Warmup: {}", args.iterations, args.warmup).dimmed()
            );
        }

        let result = run_single_benchmark(operation, args.iterations, args.warmup)?;
        results.push(result);
    } else {
        // Run all benchmarks
        if !args.json {
            println!("{}\n", "Running all benchmarks".blue());
            println!(
                "{}\n",
                format!("Iterations: {}, Warmup: {}", args.iterations, args.warmup).dimmed()
            );
        }

        for bench in BENCHMARKS {
            if !args.json {
                print!("  {}...", bench.name);
            }

            match run_single_benchmark(bench.name, args.iterations, args.warmup) {
                Ok(result) => {
                    results.push(result);
                    if !args.json {
                        println!("{}", " done".green());
                    }
                }
                Err(e) => {
                    if !args.json {
                        println!("{}", format!(" failed: {}", e).red());
                    }
                }
            }
        }

        if !args.json {
            println!();
        }
    }

    // Output results
    if args.json {
        println!("{}", serde_json::to_string_pretty(&results)?);
    } else {
        println!("{}", format_results(&results));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── calculate_stats ─────────────────────────────────────────

    #[test]
    fn test_calculate_stats_basic() {
        let durations = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let (min, max, avg, p50, p95, std_dev) = calculate_stats(&durations);

        assert!((min - 1.0).abs() < f64::EPSILON);
        assert!((max - 5.0).abs() < f64::EPSILON);
        assert!((avg - 3.0).abs() < f64::EPSILON);
        assert!((p50 - 3.0).abs() < f64::EPSILON);
        assert!((p95 - 5.0).abs() < f64::EPSILON);
        assert!(std_dev > 0.0);
    }

    #[test]
    fn test_calculate_stats_single_value() {
        let durations = vec![42.0];
        let (min, max, avg, p50, p95, std_dev) = calculate_stats(&durations);

        assert!((min - 42.0).abs() < f64::EPSILON);
        assert!((max - 42.0).abs() < f64::EPSILON);
        assert!((avg - 42.0).abs() < f64::EPSILON);
        assert!((p50 - 42.0).abs() < f64::EPSILON);
        assert!((p95 - 42.0).abs() < f64::EPSILON);
        assert!((std_dev - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_calculate_stats_empty() {
        let durations: Vec<f64> = vec![];
        let (min, max, avg, p50, p95, std_dev) = calculate_stats(&durations);

        assert!((min - 0.0).abs() < f64::EPSILON);
        assert!((max - 0.0).abs() < f64::EPSILON);
        assert!((avg - 0.0).abs() < f64::EPSILON);
        assert!((p50 - 0.0).abs() < f64::EPSILON);
        assert!((p95 - 0.0).abs() < f64::EPSILON);
        assert!((std_dev - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_calculate_stats_unsorted_input() {
        let durations = vec![5.0, 1.0, 3.0, 2.0, 4.0];
        let (min, max, avg, _, _, _) = calculate_stats(&durations);

        assert!((min - 1.0).abs() < f64::EPSILON);
        assert!((max - 5.0).abs() < f64::EPSILON);
        assert!((avg - 3.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_calculate_stats_identical_values() {
        let durations = vec![7.0, 7.0, 7.0, 7.0];
        let (min, max, avg, p50, p95, std_dev) = calculate_stats(&durations);

        assert!((min - 7.0).abs() < f64::EPSILON);
        assert!((max - 7.0).abs() < f64::EPSILON);
        assert!((avg - 7.0).abs() < f64::EPSILON);
        assert!((p50 - 7.0).abs() < f64::EPSILON);
        assert!((p95 - 7.0).abs() < f64::EPSILON);
        assert!((std_dev - 0.0).abs() < f64::EPSILON);
    }

    // ── format_duration ─────────────────────────────────────────

    #[test]
    fn test_format_duration_nanoseconds() {
        let result = format_duration(0.0005);
        assert!(result.contains("ns"), "expected ns, got: {}", result);
    }

    #[test]
    fn test_format_duration_microseconds() {
        let result = format_duration(0.5);
        assert!(result.contains("µs"), "expected µs, got: {}", result);
    }

    #[test]
    fn test_format_duration_milliseconds() {
        let result = format_duration(42.5);
        assert!(result.contains("ms"), "expected ms, got: {}", result);
        assert_eq!(result, "42.50ms");
    }

    #[test]
    fn test_format_duration_seconds() {
        let result = format_duration(1500.0);
        assert!(result.contains("s"), "expected s, got: {}", result);
        assert_eq!(result, "1.50s");
    }

    #[test]
    fn test_format_duration_minutes() {
        let result = format_duration(90000.0);
        assert!(result.contains("m"), "expected m, got: {}", result);
    }

    // ── run_benchmark_operation ─────────────────────────────────

    #[test]
    fn test_benchmark_manifest_parse() {
        let result = run_benchmark_operation("manifest-parse");
        assert!(result.is_ok(), "manifest-parse failed: {:?}", result.err());
    }

    #[test]
    fn test_benchmark_state_parse() {
        let result = run_benchmark_operation("state-parse");
        assert!(result.is_ok(), "state-parse failed: {:?}", result.err());
    }

    #[test]
    fn test_benchmark_url_parse() {
        let result = run_benchmark_operation("url-parse");
        assert!(result.is_ok(), "url-parse failed: {:?}", result.err());
    }

    #[test]
    fn test_benchmark_unknown_operation() {
        let result = run_benchmark_operation("nonexistent");
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("Unknown benchmark"),
            "error should mention unknown: {}",
            err
        );
    }

    // ── run_single_benchmark ────────────────────────────────────

    #[test]
    fn test_run_single_benchmark() {
        let result = run_single_benchmark("manifest-parse", 5, 1);
        assert!(result.is_ok(), "benchmark failed: {:?}", result.err());
        let bench = result.unwrap();
        assert_eq!(bench.name, "manifest-parse");
        assert_eq!(bench.iterations, 5);
        assert!(bench.min > 0.0);
        assert!(bench.max >= bench.min);
        assert!(bench.avg >= bench.min);
        assert!(bench.avg <= bench.max);
    }

    // ── format_results ──────────────────────────────────────────

    #[test]
    fn test_format_results_contains_header() {
        let results = vec![BenchmarkResult {
            name: "test-op".to_string(),
            iterations: 10,
            min: 0.5,
            max: 1.5,
            avg: 1.0,
            p50: 0.9,
            p95: 1.4,
            std_dev: 0.3,
        }];

        let output = format_results(&results);
        assert!(output.contains("Benchmark Results"));
        assert!(output.contains("test-op"));
        assert!(output.contains("10")); // iterations
    }

    // ── BenchmarkResult serde ───────────────────────────────────

    #[test]
    fn test_benchmark_result_json_serialization() {
        let result = BenchmarkResult {
            name: "test".to_string(),
            iterations: 10,
            min: 0.1,
            max: 0.5,
            avg: 0.3,
            p50: 0.25,
            p95: 0.45,
            std_dev: 0.1,
        };

        let json = serde_json::to_string(&result).unwrap();
        assert!(json.contains("\"name\":\"test\""));
        assert!(json.contains("\"iterations\":10"));
    }

    // ── BENCHMARKS constant ─────────────────────────────────────

    #[test]
    fn test_benchmarks_list_not_empty() {
        assert!(
            !BENCHMARKS.is_empty(),
            "BENCHMARKS should have at least one entry"
        );
        for bench in BENCHMARKS {
            assert!(!bench.name.is_empty());
            assert!(!bench.description.is_empty());
        }
    }
}
