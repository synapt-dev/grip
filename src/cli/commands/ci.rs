//! CI pipeline command implementation
//!
//! Provides `gr ci run`, `gr ci list`, and `gr ci status` for workspace CI/CD.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Command;
use std::time::Instant;

/// Result of a single CI step
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepResult {
    pub name: String,
    pub command: String,
    pub success: bool,
    pub exit_code: Option<i32>,
    pub duration_ms: u64,
    pub output: String,
}

/// Result of a pipeline run
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineResult {
    pub pipeline: String,
    pub success: bool,
    pub steps: Vec<StepResult>,
    pub total_duration_ms: u64,
    pub timestamp: String,
}

/// Run a CI pipeline
pub fn run_ci_run(
    workspace_root: &Path,
    manifest: &Manifest,
    pipeline_name: &str,
    json: bool,
) -> anyhow::Result<()> {
    let ci_config = manifest
        .workspace
        .as_ref()
        .and_then(|w| w.ci.as_ref())
        .and_then(|ci| ci.pipelines.as_ref());

    let pipelines = match ci_config {
        Some(p) => p,
        None => {
            anyhow::bail!(
                "No CI pipelines defined in manifest. Add a 'workspace.ci.pipelines' section."
            );
        }
    };

    let pipeline = match pipelines.get(pipeline_name) {
        Some(p) => p,
        None => {
            let available: Vec<&String> = pipelines.keys().collect();
            anyhow::bail!(
                "Pipeline '{}' not found. Available: {:?}",
                pipeline_name,
                available
            );
        }
    };

    if !json {
        Output::header(&format!("Running pipeline: {}", pipeline_name));
        if let Some(ref desc) = pipeline.description {
            Output::info(desc);
        }
        println!();
    }

    let pipeline_start = Instant::now();
    let mut step_results: Vec<StepResult> = Vec::new();
    let mut pipeline_success = true;

    for step in &pipeline.steps {
        if !json {
            let spinner = Output::spinner(&format!("Running: {}...", step.name));

            let result = run_step(workspace_root, step, manifest);
            let step_result = result;

            if step_result.success {
                spinner.finish_with_message(format!(
                    "{}: passed ({}ms)",
                    step.name, step_result.duration_ms
                ));
            } else {
                spinner.finish_with_message(format!(
                    "{}: FAILED (exit {})",
                    step.name,
                    step_result.exit_code.unwrap_or(-1)
                ));

                if !step_result.output.is_empty() {
                    eprintln!("{}", step_result.output);
                }

                if !step.continue_on_error {
                    pipeline_success = false;
                    step_results.push(step_result);
                    break;
                }
                pipeline_success = false;
            }

            step_results.push(step_result);
        } else {
            let step_result = run_step(workspace_root, step, manifest);
            let failed = !step_result.success;
            step_results.push(step_result);

            if failed && !step.continue_on_error {
                pipeline_success = false;
                break;
            }
            if failed {
                pipeline_success = false;
            }
        }
    }

    let total_duration_ms = pipeline_start.elapsed().as_millis() as u64;

    let result = PipelineResult {
        pipeline: pipeline_name.to_string(),
        success: pipeline_success,
        steps: step_results,
        total_duration_ms,
        timestamp: Utc::now().to_rfc3339(),
    };

    // Save result to disk
    save_ci_result(workspace_root, &result)?;

    if json {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if pipeline_success {
            Output::success(&format!(
                "Pipeline '{}' passed ({}ms)",
                pipeline_name, total_duration_ms
            ));
        } else {
            Output::error(&format!(
                "Pipeline '{}' failed ({}ms)",
                pipeline_name, total_duration_ms
            ));
        }
    }

    if !pipeline_success {
        anyhow::bail!("Pipeline '{}' failed", result.pipeline);
    }

    Ok(())
}

/// List available CI pipelines
pub fn run_ci_list(manifest: &Manifest, json: bool) -> anyhow::Result<()> {
    let ci_config = manifest
        .workspace
        .as_ref()
        .and_then(|w| w.ci.as_ref())
        .and_then(|ci| ci.pipelines.as_ref());

    let pipelines = match ci_config {
        Some(p) => p,
        None => {
            if json {
                println!("[]");
            } else {
                println!("No CI pipelines defined.");
            }
            return Ok(());
        }
    };

    if json {
        #[derive(Serialize)]
        struct PipelineInfo {
            name: String,
            description: Option<String>,
            steps: usize,
        }

        let infos: Vec<PipelineInfo> = pipelines
            .iter()
            .map(|(name, pipeline)| PipelineInfo {
                name: name.clone(),
                description: pipeline.description.clone(),
                steps: pipeline.steps.len(),
            })
            .collect();

        println!("{}", serde_json::to_string_pretty(&infos)?);
    } else {
        Output::header("CI Pipelines");
        println!();

        for (name, pipeline) in pipelines {
            let desc = pipeline
                .description
                .as_deref()
                .unwrap_or("(no description)");
            println!("  {} - {} ({} steps)", name, desc, pipeline.steps.len());
        }
    }

    Ok(())
}

/// Show status of last CI runs
pub fn run_ci_status(workspace_root: &Path, json: bool) -> anyhow::Result<()> {
    let results_dir = workspace_root.join(".gitgrip").join("ci-results");

    if !results_dir.exists() {
        if json {
            println!("[]");
        } else {
            println!("No CI results found. Run 'gr ci run <pipeline>' first.");
        }
        return Ok(());
    }

    let mut results: Vec<PipelineResult> = Vec::new();

    for entry in std::fs::read_dir(&results_dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("json") {
            let content = std::fs::read_to_string(&path)?;
            if let Ok(result) = serde_json::from_str::<PipelineResult>(&content) {
                results.push(result);
            }
        }
    }

    // Sort by timestamp (most recent first)
    results.sort_by(|a, b| b.timestamp.cmp(&a.timestamp));

    if json {
        println!("{}", serde_json::to_string_pretty(&results)?);
    } else {
        Output::header("CI Status");
        println!();

        if results.is_empty() {
            println!("  No results found.");
        } else {
            for result in &results {
                let status = if result.success { "PASS" } else { "FAIL" };
                let passed = result.steps.iter().filter(|s| s.success).count();
                let total = result.steps.len();
                println!(
                    "  {} {} - {} ({}/{} steps, {}ms) [{}]",
                    if result.success { "✓" } else { "✗" },
                    result.pipeline,
                    status,
                    passed,
                    total,
                    result.total_duration_ms,
                    result.timestamp
                );
            }
        }
    }

    Ok(())
}

/// Run a single CI step
fn run_step(
    workspace_root: &Path,
    step: &crate::core::manifest::CiStep,
    manifest: &Manifest,
) -> StepResult {
    let start = Instant::now();

    // Resolve working directory
    let cwd = match &step.cwd {
        Some(dir) => workspace_root.join(dir),
        None => workspace_root.to_path_buf(),
    };

    let mut cmd = Command::new("sh");
    cmd.arg("-c").arg(&step.command).current_dir(&cwd);

    // Set workspace env vars
    if let Some(ref workspace) = manifest.workspace {
        if let Some(ref env) = workspace.env {
            for (key, value) in env {
                cmd.env(key, value);
            }
        }
    }

    // Set step-specific env vars
    if let Some(ref env) = step.env {
        for (key, value) in env {
            cmd.env(key, value);
        }
    }

    match cmd.output() {
        Ok(output) => {
            let duration_ms = start.elapsed().as_millis() as u64;
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            let combined = if stderr.is_empty() {
                stdout.to_string()
            } else {
                format!("{}\n{}", stdout, stderr)
            };

            StepResult {
                name: step.name.clone(),
                command: step.command.clone(),
                success: output.status.success(),
                exit_code: output.status.code(),
                duration_ms,
                output: combined,
            }
        }
        Err(e) => StepResult {
            name: step.name.clone(),
            command: step.command.clone(),
            success: false,
            exit_code: None,
            duration_ms: start.elapsed().as_millis() as u64,
            output: format!("Failed to execute: {}", e),
        },
    }
}

/// Save CI result to disk
fn save_ci_result(workspace_root: &Path, result: &PipelineResult) -> anyhow::Result<()> {
    let results_dir = workspace_root.join(".gitgrip").join("ci-results");
    std::fs::create_dir_all(&results_dir)?;

    let filename = format!("{}.json", result.pipeline);
    let path = results_dir.join(filename);
    let json = serde_json::to_string_pretty(result)?;
    std::fs::write(path, json)?;

    Ok(())
}
