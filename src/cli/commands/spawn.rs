//! Spawn command implementation
//!
//! Multi-agent orchestration: reads an agents.toml config file and
//! launches AI agents in tmux windows.

use crate::cli::output::Output;
use colored::Colorize;
use serde::Deserialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

// ---------------------------------------------------------------------------
// Config types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct SpawnConfig {
    pub spawn: SpawnGlobal,
    pub agents: HashMap<String, AgentConfig>,
}

#[derive(Deserialize)]
pub struct SpawnGlobal {
    #[serde(default = "default_session")]
    pub session_name: String,
    #[serde(default = "default_channel")]
    pub channel: String,
    #[serde(default)]
    pub auto_journal: bool,
    #[serde(default)]
    pub mock_launch: bool,
}

fn default_session() -> String {
    "synapt".into()
}
fn default_channel() -> String {
    "dev".into()
}

#[derive(Deserialize)]
pub struct AgentConfig {
    pub role: String,
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "default_tool")]
    pub tool: String,
    #[serde(default = "default_worktree")]
    pub worktree: String,
    pub startup_prompt: Option<String>,
    pub channel: Option<String>,
    #[serde(default = "default_loop")]
    pub loop_interval: String,
    #[serde(default = "default_heartbeat")]
    pub heartbeat_interval: u64,
    #[serde(default = "default_timeout")]
    pub timeout_threshold: u64,
    #[serde(default = "default_restart_policy")]
    pub restart_policy: String,
    #[serde(default = "default_restart_delay")]
    pub restart_delay: u64,
    #[serde(default = "default_max_restarts")]
    pub max_restarts: u64,
    #[serde(default)]
    pub env: HashMap<String, String>,
}

fn default_model() -> String {
    "claude-sonnet-4-6".into()
}
fn default_tool() -> String {
    "claude".into()
}
fn default_worktree() -> String {
    "main".into()
}
fn default_loop() -> String {
    "5m".into()
}
fn default_heartbeat() -> u64 {
    60
}
fn default_timeout() -> u64 {
    180
}
fn default_restart_policy() -> String {
    "always".into()
}
fn default_restart_delay() -> u64 {
    5
}
fn default_max_restarts() -> u64 {
    3
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Find the workspace root by walking up from the current directory.
fn find_workspace_root() -> anyhow::Result<PathBuf> {
    let mut dir = std::env::current_dir()?;
    loop {
        if dir.join(".gitgrip").exists() {
            return Ok(dir);
        }
        match dir.parent() {
            Some(parent) => dir = parent.to_path_buf(),
            None => anyhow::bail!("Not in a gitgrip workspace (no .gitgrip directory found)"),
        }
    }
}

/// Load and parse the spawn config from the given path (or default).
fn load_config(config_path: Option<&str>) -> anyhow::Result<(SpawnConfig, PathBuf)> {
    let workspace_root = find_workspace_root()?;
    let path = match config_path {
        Some(p) => PathBuf::from(p),
        None => workspace_root.join(".gitgrip").join("agents.toml"),
    };

    if !path.exists() {
        anyhow::bail!(
            "Spawn config not found at {}\n\n\
             Create one at .gitgrip/agents.toml to configure agents.\n\
             See `gr spawn list` after creating the config.",
            path.display()
        );
    }

    let content = std::fs::read_to_string(&path)?;
    let config: SpawnConfig = toml::from_str(&content)
        .map_err(|e| anyhow::anyhow!("Failed to parse {}: {}", path.display(), e))?;

    Ok((config, workspace_root))
}

/// Check that tmux is available on the system.
fn require_tmux() -> anyhow::Result<()> {
    let output = Command::new("tmux").arg("-V").output();
    match output {
        Ok(o) if o.status.success() => Ok(()),
        _ => anyhow::bail!(
            "tmux is required for `gr spawn` but was not found.\n\
             Install it with: brew install tmux (macOS) or apt install tmux (Linux)"
        ),
    }
}

/// Check if a tmux session exists.
fn session_exists(session_name: &str) -> bool {
    Command::new("tmux")
        .args(["has-session", "-t", session_name])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Create a tmux session (detached).
fn create_session(session_name: &str) -> anyhow::Result<()> {
    let status = Command::new("tmux")
        .args(["new-session", "-d", "-s", session_name])
        .status()?;
    if !status.success() {
        anyhow::bail!("Failed to create tmux session '{}'", session_name);
    }
    Ok(())
}

/// Get sorted agent names for deterministic ordering.
fn sorted_agent_names(agents: &HashMap<String, AgentConfig>) -> Vec<String> {
    let mut names: Vec<String> = agents.keys().cloned().collect();
    names.sort();
    names
}

// ---------------------------------------------------------------------------
// Subcommand: up
// ---------------------------------------------------------------------------

pub fn run_spawn_up(
    agent_filter: Option<String>,
    config_path: Option<String>,
    force_mock: bool,
    _quiet: bool,
    _json: bool,
) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, workspace_root) = load_config(config_path.as_deref())?;
    let session = &config.spawn.session_name;
    let mock_mode = force_mock || config.spawn.mock_launch;

    // Ensure tmux session exists
    if !session_exists(session) {
        create_session(session)?;
        Output::info(&format!("Created tmux session '{}'", session));
    }

    let names = sorted_agent_names(&config.agents);
    let targets: Vec<&str> = match &agent_filter {
        Some(name) => {
            if !config.agents.contains_key(name) {
                anyhow::bail!(
                    "Agent '{}' not found in config. Available: {}",
                    name,
                    names.join(", ")
                );
            }
            vec![name.as_str()]
        }
        None => names.iter().map(|s| s.as_str()).collect(),
    };

    println!();
    Output::header(&format!(
        "Launching {} agent{}{}...",
        targets.len(),
        if targets.len() == 1 { "" } else { "s" },
        if mock_mode { " (mock)" } else { "" }
    ));
    println!();

    for name in &targets {
        let agent = &config.agents[*name];
        let channel = agent.channel.as_deref().unwrap_or(&config.spawn.channel);

        // Create window
        let target = format!("{}:{}", session, name);
        let status = Command::new("tmux")
            .args(["new-window", "-t", session, "-n", name])
            .status()?;
        if !status.success() {
            Output::error(&format!("Failed to create window for {}", name));
            continue;
        }

        // Set remain-on-exit so dead panes stay visible
        let _ = Command::new("tmux")
            .args(["set-option", "-t", &target, "remain-on-exit", "on"])
            .status();

        // Send environment variables
        let env_cmd = format!(
            "export AGENT_NAME={} AGENT_ROLE=\"{}\" SYNAPT_CHANNELS={} SYNAPT_LOOP_INTERVAL={}",
            name, agent.role, channel, agent.loop_interval
        );
        let _ = Command::new("tmux")
            .args(["send-keys", "-t", &target, &env_cmd, "Enter"])
            .status();

        // Send any custom env vars
        for (key, val) in &agent.env {
            let custom_env = format!("export {}=\"{}\"", key, val);
            let _ = Command::new("tmux")
                .args(["send-keys", "-t", &target, &custom_env, "Enter"])
                .status();
        }

        // Build and send launch command
        let launch_cmd = if mock_mode {
            format!(
                "echo \"Agent {} would launch here (role: {}, model: {})\" && sleep 86400",
                name, agent.role, agent.model
            )
        } else {
            let worktree_path = resolve_worktree_path(&workspace_root, &agent.worktree);
            let mut cmd = format!("cd {} && {}", worktree_path.display(), agent.tool);
            cmd.push_str(&format!(" --model {}", agent.model));
            if let Some(ref prompt_path) = agent.startup_prompt {
                cmd.push_str(&format!(" --prompt \"$(cat {})\"", prompt_path));
            }
            cmd
        };

        let _ = Command::new("tmux")
            .args(["send-keys", "-t", &target, &launch_cmd, "Enter"])
            .status();

        // Print status
        let mode_tag = if mock_mode {
            " [mock]".dimmed().to_string()
        } else {
            String::new()
        };
        println!(
            "  {} {} ({}) launched in {}{}",
            "✓".green(),
            name.bold(),
            agent.role.dimmed(),
            target.cyan(),
            mode_tag,
        );
    }

    println!();
    Output::info(&format!("Attach with: tmux attach -t {}", session));

    Ok(())
}

/// Resolve a worktree identifier to an absolute path.
fn resolve_worktree_path(workspace_root: &Path, worktree: &str) -> PathBuf {
    if worktree == "main" {
        workspace_root.to_path_buf()
    } else {
        // Griptrees live as siblings, e.g. ../feat-auth/
        let sanitised = worktree.replace('/', "-");
        workspace_root
            .parent()
            .unwrap_or(workspace_root)
            .join(sanitised)
    }
}

// ---------------------------------------------------------------------------
// Subcommand: status
// ---------------------------------------------------------------------------

pub fn run_spawn_status(_quiet: bool, _json: bool) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, _workspace_root) = load_config(None)?;
    let session = &config.spawn.session_name;

    println!();
    Output::header(&format!("Agent status (session: {})...", session));
    println!();

    if !session_exists(session) {
        Output::warning(&format!("Session '{}' does not exist", session));
        return Ok(());
    }

    // List windows in the session
    let windows_output = Command::new("tmux")
        .args(["list-windows", "-t", session, "-F", "#{window_name}"])
        .output()?;
    let active_windows: Vec<String> = String::from_utf8_lossy(&windows_output.stdout)
        .lines()
        .map(|l| l.to_string())
        .collect();

    let names = sorted_agent_names(&config.agents);
    for name in &names {
        let agent = &config.agents[name];
        if active_windows.contains(name) {
            // Check pane status
            let target = format!("{}:{}", session, name);
            let pane_output = Command::new("tmux")
                .args([
                    "list-panes",
                    "-t",
                    &target,
                    "-F",
                    "#{pane_pid} #{pane_dead}",
                ])
                .output()?;
            let pane_info = String::from_utf8_lossy(&pane_output.stdout);
            let first_line = pane_info.lines().next().unwrap_or("");
            let parts: Vec<&str> = first_line.split_whitespace().collect();

            if parts.len() >= 2 && parts[1] == "1" {
                println!(
                    "  {} {}: {} ({})",
                    "✗".red(),
                    name.bold(),
                    "dead".red(),
                    agent.role.dimmed(),
                );
            } else if !parts.is_empty() {
                println!(
                    "  {} {}: {} (pid {}) ({})",
                    "✓".green(),
                    name.bold(),
                    "running".green(),
                    parts[0],
                    agent.role.dimmed(),
                );
            } else {
                println!(
                    "  {} {}: {} ({})",
                    "?".yellow(),
                    name.bold(),
                    "unknown".yellow(),
                    agent.role.dimmed(),
                );
            }
        } else {
            println!(
                "  {} {}: {} ({})",
                "-".dimmed(),
                name.bold(),
                "not started".dimmed(),
                agent.role.dimmed(),
            );
        }
    }

    println!();
    Ok(())
}

// ---------------------------------------------------------------------------
// Subcommand: down
// ---------------------------------------------------------------------------

pub fn run_spawn_down(
    agent_filter: Option<String>,
    _quiet: bool,
    _json: bool,
) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, workspace_root) = load_config(None)?;
    let session = &config.spawn.session_name;

    if !session_exists(session) {
        Output::warning(&format!("Session '{}' does not exist", session));
        return Ok(());
    }

    let names = sorted_agent_names(&config.agents);
    let targets: Vec<&str> = match &agent_filter {
        Some(name) => {
            if !config.agents.contains_key(name) {
                anyhow::bail!(
                    "Agent '{}' not found in config. Available: {}",
                    name,
                    names.join(", ")
                );
            }
            vec![name.as_str()]
        }
        None => names.iter().map(|s| s.as_str()).collect(),
    };

    println!();
    Output::header(&format!(
        "Stopping {} agent{}...",
        targets.len(),
        if targets.len() == 1 { "" } else { "s" }
    ));
    println!();

    // Write spawn state before killing
    write_spawn_state(&workspace_root, &targets)?;

    for name in &targets {
        let target = format!("{}:{}", session, name);
        let status = Command::new("tmux")
            .args(["kill-window", "-t", &target])
            .status()?;
        if status.success() {
            println!("  {} {} stopped", "✗".red(), name.bold());
        } else {
            println!("  {} {} (window not found)", "-".dimmed(), name.bold(),);
        }
    }

    // If we stopped all agents, optionally kill the session
    if agent_filter.is_none() {
        // Check if any windows remain (besides the default)
        let windows_output = Command::new("tmux")
            .args(["list-windows", "-t", session, "-F", "#{window_name}"])
            .output();
        let remaining = windows_output
            .map(|o| String::from_utf8_lossy(&o.stdout).lines().count())
            .unwrap_or(0);
        if remaining <= 1 {
            let _ = Command::new("tmux")
                .args(["kill-session", "-t", session])
                .status();
            Output::info(&format!("Session '{}' terminated", session));
        }
    }

    println!();
    Ok(())
}

/// Write intentional stop state to .synapt/recall/spawn_state.json
fn write_spawn_state(workspace_root: &Path, agents: &[&str]) -> anyhow::Result<()> {
    let state_dir = workspace_root.join(".synapt").join("recall");
    std::fs::create_dir_all(&state_dir)?;
    let state_path = state_dir.join("spawn_state.json");

    let timestamp = chrono::Utc::now().to_rfc3339();
    let state = serde_json::json!({
        "action": "stop",
        "agents": agents,
        "timestamp": timestamp,
        "intentional": true
    });

    std::fs::write(&state_path, serde_json::to_string_pretty(&state)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Subcommand: list
// ---------------------------------------------------------------------------

pub fn run_spawn_list(_quiet: bool, _json: bool) -> anyhow::Result<()> {
    let (config, _workspace_root) = load_config(None)?;

    println!();
    Output::header("Configured agents:");
    println!();

    // Print table header
    println!(
        "  {:<14} {:<24} {:<22} {:<10}",
        "NAME".bold(),
        "ROLE".bold(),
        "MODEL".bold(),
        "TOOL".bold(),
    );
    println!(
        "  {:<14} {:<24} {:<22} {:<10}",
        "----", "----", "-----", "----"
    );

    let names = sorted_agent_names(&config.agents);
    for name in &names {
        let agent = &config.agents[name];
        println!(
            "  {:<14} {:<24} {:<22} {:<10}",
            name.cyan(),
            agent.role,
            agent.model.dimmed(),
            agent.tool.dimmed(),
        );
    }

    println!();
    Output::info(&format!(
        "Session: {}  |  Channel: {}  |  Mock: {}",
        config.spawn.session_name, config.spawn.channel, config.spawn.mock_launch,
    ));
    println!();

    Ok(())
}
