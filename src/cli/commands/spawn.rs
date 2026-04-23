//! Spawn command implementation
//!
//! Multi-agent orchestration: reads an agents.toml config file and
//! launches AI agents in tmux windows.

use crate::cli::output::Output;
use colored::Colorize;
use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::process::Command;

const AGENT_HISTORY_LIMIT: &str = "50000";

// ---------------------------------------------------------------------------
// Config types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct SpawnConfig {
    pub spawn: SpawnGlobal,
    #[serde(default)]
    pub tools: HashMap<String, ToolConfig>,
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
    /// Global environment variables injected into all agent sessions.
    #[serde(default)]
    pub env: HashMap<String, String>,
    /// Org ID for the agent registry. Defaults to session_name if not set.
    #[serde(default)]
    pub org_id: Option<String>,
}

#[derive(Deserialize, Clone)]
pub struct ToolConfig {
    pub binary: String,
    #[serde(default)]
    pub cmd: Vec<String>,
    #[serde(default)]
    pub default_args: Vec<String>,
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
    #[serde(default)]
    pub cmd: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
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
pub(crate) fn find_workspace_root() -> anyhow::Result<PathBuf> {
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

fn agent_window_tmux_options() -> [(&'static str, &'static str); 2] {
    [("remain-on-exit", "on"), ("history-limit", AGENT_HISTORY_LIMIT)]
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

        // Keep exited panes visible and retain enough scrollback for dashboard review.
        for (option, value) in agent_window_tmux_options() {
            let _ = Command::new("tmux")
                .args(["set-option", "-t", &target, option, value])
                .status();
        }

        // Register agent in org registry and get stable ID (#510)
        let org_id = config
            .spawn
            .org_id
            .as_deref()
            .unwrap_or(&config.spawn.session_name);
        let org_dir = crate::core::agent_registry::org_dir(org_id);
        let agent_id = match crate::core::agent_registry::get_agent_by_name(&org_dir, org_id, name)
        {
            Ok(Some(entry)) => entry.agent_id,
            _ => {
                match crate::core::agent_registry::register_agent(
                    &org_dir,
                    org_id,
                    name,
                    Some(&agent.role),
                ) {
                    Ok(id) => id,
                    Err(e) => {
                        Output::warning(&format!("Failed to register agent '{}': {}", name, e));
                        format!("{}-000", name.to_lowercase())
                    }
                }
            }
        };

        // Consolidate env vars into one export. Priority: agent.env > spawn.env > hardcoded.
        let grip_root = workspace_root.display();
        let mut env_vars: BTreeMap<String, String> = BTreeMap::new();
        env_vars.insert("GRIPSPACE_ROOT".into(), grip_root.to_string());
        env_vars.insert("SYNAPT_AGENT_ID".into(), agent_id.clone());
        env_vars.insert("AGENT_NAME".into(), name.to_string());
        env_vars.insert("AGENT_ROLE".into(), agent.role.clone());
        env_vars.insert("SYNAPT_CHANNELS".into(), channel.to_string());
        env_vars.insert("SYNAPT_LOOP_INTERVAL".into(), agent.loop_interval.clone());
        for (key, val) in &config.spawn.env {
            env_vars.insert(key.clone(), val.clone());
        }
        for (key, val) in &agent.env {
            env_vars.insert(key.clone(), val.clone());
        }
        let pairs: Vec<String> = env_vars
            .iter()
            .map(|(k, v)| format!("{}=\"{}\"", k, v))
            .collect();
        let env_cmd = format!("export {}", pairs.join(" "));
        let _ = Command::new("tmux")
            .args(["send-keys", "-t", &target, &env_cmd, "Enter"])
            .status();

        // Build and send launch command
        let launch_cmd = if mock_mode {
            format!(
                "echo \"Agent {} would launch here (role: {}, model: {})\" && sleep 86400",
                name, agent.role, agent.model
            )
        } else {
            let worktree_path = resolve_worktree_path(&workspace_root, &agent.worktree);

            // Resolve tool config
            let tool_config = config.tools.get(&agent.tool);
            let binary = tool_config
                .map(|t| t.binary.as_str())
                .unwrap_or(&agent.tool);

            // Build: binary + cmd + args + default_args
            // cmd: agent cmd overrides tool cmd (if agent has it)
            let cmd_parts: &[String] = if !agent.cmd.is_empty() {
                &agent.cmd
            } else {
                tool_config.map(|t| t.cmd.as_slice()).unwrap_or(&[])
            };

            // default_args from tool config (appended last)
            let default_args: &[String] = tool_config
                .map(|t| t.default_args.as_slice())
                .unwrap_or(&[]);

            // Resolve relative paths against gripspace root
            // (griptrees don't have .gitgrip/, so paths need to be absolute)
            let resolve = |arg: &str| -> String {
                if arg.starts_with(".gitgrip/") || arg.starts_with("prompts/") {
                    workspace_root.join(arg).display().to_string()
                } else {
                    arg.to_string()
                }
            };

            let resolved_defaults: Vec<String> = default_args.iter().map(|s| resolve(s)).collect();
            let resolved_args: Vec<String> = agent.args.iter().map(|s| resolve(s)).collect();

            // Strip --resume when no prior session exists (#579)
            let has_resume = resolved_defaults.iter().any(|a| a == "--resume")
                || resolved_args.iter().any(|a| a == "--resume");
            let resolved_defaults: Vec<String> =
                if has_resume && !has_claude_session(&worktree_path) {
                    Output::info(&format!(
                        "  {} stripping --resume (no prior session for {})",
                        name,
                        worktree_path.display()
                    ));
                    resolved_defaults
                        .into_iter()
                        .filter(|a| a != "--resume")
                        .collect()
                } else {
                    resolved_defaults
                };
            let resolved_args: Vec<String> = if has_resume && !has_claude_session(&worktree_path) {
                resolved_args
                    .into_iter()
                    .filter(|a| a != "--resume")
                    .collect()
            } else {
                resolved_args
            };

            // Inject --model from agent.model if not already in args (#472)
            let has_model_flag = resolved_args.iter().any(|a| a == "--model")
                || resolved_defaults.iter().any(|a| a == "--model");
            let model_inject: Vec<String> = if !has_model_flag && !agent.model.is_empty() {
                vec!["--model".into(), agent.model.clone()]
            } else {
                vec![]
            };

            let mut parts: Vec<&str> = vec![binary];
            parts.extend(cmd_parts.iter().map(|s| s.as_str()));
            parts.extend(resolved_defaults.iter().map(|s| s.as_str()));
            parts.extend(model_inject.iter().map(|s| s.as_str()));
            parts.extend(resolved_args.iter().map(|s| s.as_str()));

            let launch = parts.join(" ");
            format!("cd {} && {}", worktree_path.display(), launch)
        };

        let _ = Command::new("tmux")
            .args(["send-keys", "-t", &target, &launch_cmd, "Enter"])
            .status();

        // Set up pipe-pane for output streaming (#443 Mission Control)
        let log_dir = workspace_root.join(".synapt").join("logs").join(&agent_id);
        let _ = std::fs::create_dir_all(&log_dir);
        let log_path = log_dir.join("output.log");
        let pipe_cmd = format!("cat >> {}", log_path.display());
        let _ = Command::new("tmux")
            .args(["pipe-pane", "-t", &target, &pipe_cmd])
            .status();

        // Get tmux pane PID for process tracking
        let pane_pid = Command::new("tmux")
            .args(["display-message", "-t", &target, "-p", "#{pane_pid}"])
            .output()
            .ok()
            .and_then(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .trim()
                    .parse::<u32>()
                    .ok()
            });

        // Update process state in team.db
        if let Err(e) = crate::core::agent_registry::update_process_state(
            &org_dir,
            &agent_id,
            pane_pid,
            Some(&target),
            "online",
            Some(log_path.to_str().unwrap_or("")),
            None, // session_id set by agent on join
        ) {
            Output::warning(&format!(
                "Failed to update process state for {}: {}",
                name, e
            ));
        }

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
/// Check if a Claude Code session exists for the given worktree path.
///
/// Claude Code stores sessions at `~/.claude/projects/<slug>/` where the
/// slug is the absolute path with `/` replaced by `-`. A session exists
/// if any `.jsonl` file is present in that directory.
fn has_claude_session(worktree_path: &Path) -> bool {
    let home = match std::env::var("HOME") {
        Ok(h) => PathBuf::from(h),
        Err(_) => return false,
    };
    let abs = match worktree_path.canonicalize() {
        Ok(p) => p,
        Err(_) => worktree_path.to_path_buf(),
    };
    let slug = abs.display().to_string().replace('/', "-");
    let session_dir = home.join(".claude").join("projects").join(&slug);
    if !session_dir.is_dir() {
        return false;
    }
    match std::fs::read_dir(&session_dir) {
        Ok(entries) => entries
            .filter_map(|e| e.ok())
            .any(|e| e.path().extension().is_some_and(|ext| ext == "jsonl")),
        Err(_) => false,
    }
}

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
    timeout_secs: u64,
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

    // Write spawn state before shutdown
    write_spawn_state(&workspace_root, &targets)?;

    // Phase 1: Send /exit to each agent pane for graceful shutdown.
    // This asks the agent process to clean up and terminate on its own terms.
    for name in &targets {
        let target = format!("{}:{}", session, name);
        match Command::new("tmux")
            .args(["send-keys", "-t", &target, "/exit", "Enter"])
            .status()
        {
            Ok(s) if !s.success() => {
                eprintln!(
                    "  {} send-keys to {} failed (exit {})",
                    "⚠".yellow(),
                    name,
                    s.code().unwrap_or(-1)
                );
            }
            Err(e) => {
                eprintln!("  {} failed to send /exit to {}: {}", "⚠".yellow(), name, e);
            }
            _ => {}
        }
    }

    // Phase 2: Poll pane_dead every 500ms until all agents exit or timeout.
    let poll_interval = std::time::Duration::from_millis(500);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);

    #[derive(Clone, Copy, PartialEq)]
    enum PaneState {
        Running,
        Exited,
        Unknown, // tmux error; state could not be determined
    }
    let mut states: Vec<PaneState> = vec![PaneState::Running; targets.len()];

    while std::time::Instant::now() < deadline && states.contains(&PaneState::Running) {
        for (i, name) in targets.iter().enumerate() {
            if states[i] != PaneState::Running {
                continue;
            }
            match pane_exit_state(session, name) {
                Some(true) => states[i] = PaneState::Exited,
                Some(false) => {} // still running
                None => states[i] = PaneState::Unknown,
            }
        }
        if !states.contains(&PaneState::Running) {
            break;
        }
        std::thread::sleep(poll_interval);
    }

    // Phase 3: Report per-agent status and clean up tmux windows.
    // Agents that exited gracefully still need their dead pane/window removed.
    // Agents that timed out get force-killed via kill-window.
    let mut any_error = false;
    for (i, name) in targets.iter().enumerate() {
        let target = format!("{}:{}", session, name);
        let kill_result = Command::new("tmux")
            .args(["kill-window", "-t", &target])
            .output();
        let kill_ok = match &kill_result {
            Ok(o) if o.status.success() => true,
            Ok(o) => {
                let stderr = String::from_utf8_lossy(&o.stderr);
                // Window already gone is fine (agent cleaned up fully)
                stderr.contains("can't find") || stderr.contains("no server running")
            }
            Err(e) => {
                eprintln!("  {} kill-window failed for {}: {}", "⚠".yellow(), name, e);
                false
            }
        };

        match states[i] {
            PaneState::Exited => {
                println!("  {} {} exited gracefully", "✓".green(), name.bold());
            }
            PaneState::Unknown => {
                println!(
                    "  {} {} state unknown (tmux query failed){}",
                    "?".yellow(),
                    name.bold(),
                    if kill_ok { ", window cleaned up" } else { "" }
                );
                any_error = true;
            }
            PaneState::Running => {
                println!(
                    "  {} {} force-killed (did not exit within {}s)",
                    "✗".red(),
                    name.bold(),
                    timeout_secs
                );
            }
        }
    }

    // If we stopped all agents, kill the session if it's empty
    if agent_filter.is_none() {
        let windows_output = Command::new("tmux")
            .args(["list-windows", "-t", session, "-F", "#{window_name}"])
            .output();
        let remaining = match &windows_output {
            Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).lines().count(),
            _ => 0,
        };
        if remaining <= 1 {
            match Command::new("tmux")
                .args(["kill-session", "-t", session])
                .status()
            {
                Ok(s) if s.success() => {
                    Output::info(&format!("Session '{}' terminated", session));
                }
                Ok(s) => {
                    eprintln!(
                        "  {} kill-session failed (exit {})",
                        "⚠".yellow(),
                        s.code().unwrap_or(-1)
                    );
                    any_error = true;
                }
                Err(e) => {
                    eprintln!("  {} kill-session failed: {}", "⚠".yellow(), e);
                    any_error = true;
                }
            }
        }
    }

    if any_error {
        Output::warning("Some tmux operations reported errors (see warnings above)");
    }

    println!();
    Ok(())
}

/// Check the exit state of a tmux pane's process.
///
/// Returns:
/// - `Some(true)`  if the pane process has exited (pane_dead=1 or window gone)
/// - `Some(false)` if the pane process is still running
/// - `None`        if tmux itself failed (broken socket, unexpected error)
fn pane_exit_state(session: &str, window_name: &str) -> Option<bool> {
    let target = format!("{}:{}", session, window_name);
    let output = Command::new("tmux")
        .args(["list-panes", "-t", &target, "-F", "#{pane_dead}"])
        .output();
    match output {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout);
            Some(text.trim() == "1")
        }
        Ok(o) => {
            // tmux ran but returned an error. Exit code 1 with "can't find"
            // in stderr means the window is gone (process exited and
            // remain-on-exit is off). Any other error is unexpected.
            let stderr = String::from_utf8_lossy(&o.stderr);
            if stderr.contains("can't find") || stderr.contains("no server running") {
                Some(true)
            } else {
                eprintln!(
                    "  {} tmux error querying {}: {}",
                    "⚠".yellow(),
                    window_name,
                    stderr.trim()
                );
                None
            }
        }
        Err(e) => {
            eprintln!(
                "  {} failed to run tmux for {}: {}",
                "⚠".yellow(),
                window_name,
                e
            );
            None
        }
    }
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

// ---------------------------------------------------------------------------
// Attach
// ---------------------------------------------------------------------------

/// Attach to an agent's tmux window (replaces current process).
pub fn run_spawn_attach(agent: &str, _quiet: bool) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, _) = load_config(None)?;

    if !config.agents.contains_key(agent) {
        anyhow::bail!(
            "Unknown agent '{}'. Available: {}",
            agent,
            sorted_agent_names(&config.agents).join(", ")
        );
    }

    let session = &config.spawn.session_name;
    let target = format!("{}:{}", session, agent);

    // Verify the tmux window exists before attaching
    let check = Command::new("tmux")
        .args(["has-session", "-t", session])
        .output()?;

    if !check.status.success() {
        anyhow::bail!(
            "tmux session '{}' not found. Run `gr spawn up` first.",
            session
        );
    }

    let err = Command::new("tmux")
        .args(["select-window", "-t", &target])
        .status();

    if let Err(e) = err {
        anyhow::bail!("Failed to select tmux window '{}': {}", target, e);
    }

    attach_tmux_session(session)
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

/// View agent output without attaching (uses tmux capture-pane).
pub fn run_spawn_logs(
    agent: Option<&str>,
    lines: u32,
    all: bool,
    quiet: bool,
) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, _) = load_config(None)?;
    let session = &config.spawn.session_name;

    if !all && agent.is_none() {
        anyhow::bail!("Specify an agent name or use --all to show logs from all agents.");
    }

    let agents_to_show: Vec<String> = if all {
        sorted_agent_names(&config.agents)
    } else {
        let name = agent.unwrap();
        if !config.agents.contains_key(name) {
            anyhow::bail!(
                "Unknown agent '{}'. Available: {}",
                name,
                sorted_agent_names(&config.agents).join(", ")
            );
        }
        vec![name.to_string()]
    };

    for name in &agents_to_show {
        let target = format!("{}:{}", session, name);
        let line_arg = format!("-{}", lines);

        let output = Command::new("tmux")
            .args(["capture-pane", "-t", &target, "-p", "-S", &line_arg])
            .output();

        match output {
            Ok(out) if out.status.success() => {
                if !quiet {
                    if all {
                        println!("{}", format!("═══ {} ═══", name).cyan().bold());
                    }
                    print!("{}", String::from_utf8_lossy(&out.stdout));
                    if all {
                        println!();
                    }
                }
            }
            Ok(out) => {
                let stderr = String::from_utf8_lossy(&out.stderr);
                if !quiet {
                    eprintln!(
                        "  {} {}: {} (is the agent running?)",
                        "✗".red(),
                        name,
                        stderr.trim()
                    );
                }
            }
            Err(e) => {
                if !quiet {
                    eprintln!("  {} {}: {}", "✗".red(), name, e);
                }
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

/// Open a mission control dashboard: 2x2 agent grid + #dev input pane.
///
/// Layout:
/// ```text
/// ┌─────────────────┬─────────────────┐
/// │  agent 0 (tail) │  agent 1 (tail) │
/// ├─────────────────┼─────────────────┤
/// │  agent 2 (tail) │  agent 3 (tail) │
/// ├─────────────────┴─────────────────┤
/// │ #dev> _                           │
/// └───────────────────────────────────┘
/// ```
pub fn run_spawn_dashboard(_quiet: bool) -> anyhow::Result<()> {
    require_tmux()?;
    let (config, workspace_root) = load_config(None)?;
    let session = &config.spawn.session_name;

    if !session_exists(session) {
        anyhow::bail!(
            "Session '{}' not running. Run `gr spawn up` first.",
            session
        );
    }

    let names = sorted_agent_names(&config.agents);
    if names.is_empty() {
        anyhow::bail!("No agents configured.");
    }

    let dashboard_target = format!("{}:dashboard", session);

    // Kill existing dashboard window if present
    let _ = Command::new("tmux")
        .args(["kill-window", "-t", &dashboard_target])
        .status();

    // Create dashboard window
    Command::new("tmux")
        .args(["new-window", "-t", session, "-n", "dashboard"])
        .status()?;

    // Build capture-loop script for each agent pane
    let capture_script = |agent: &str| -> String {
        let target = format!("{}:{}", session, agent);
        format!(
            "while true; do clear; echo '═══ {} ═══'; tmux capture-pane -t {} -p -S -25 2>/dev/null || echo '(not running)'; sleep 1; done",
            agent, target
        )
    };

    // Build the #dev input loop using gr channel
    let gr_path = std::env::current_exe()
        .unwrap_or_else(|_| "gr".into())
        .display()
        .to_string();
    let input_script = format!(
        "cd {} && while IFS= read -rp $'\\033[36m#dev>\\033[0m ' msg; do [ -n \"$msg\" ] && {} channel post \"$msg\"; done",
        workspace_root.display(),
        gr_path
    );

    // Helper: get the active pane ID after a split or window creation.
    // Returns %N format (e.g. "%42") which is stable across splits.
    let get_pane_id = |target: &str| -> Option<String> {
        Command::new("tmux")
            .args(["display-message", "-t", target, "-p", "#{pane_id}"])
            .output()
            .ok()
            .and_then(|o| {
                let id = String::from_utf8_lossy(&o.stdout).trim().to_string();
                if id.starts_with('%') {
                    Some(id)
                } else {
                    None
                }
            })
    };

    // Track pane IDs for stable targeting (#452)
    let mut pane_ids: Vec<String> = Vec::new();

    // First pane (initial pane in the new window) gets agent 0
    if let Some(name) = names.first() {
        if let Some(id) = get_pane_id(&dashboard_target) {
            pane_ids.push(id.clone());
            Command::new("tmux")
                .args(["send-keys", "-t", &id, &capture_script(name), "Enter"])
                .status()?;
        }
    }

    // Split right for agent 1
    if names.len() > 1 {
        if let Some(ref first_pane) = pane_ids.first().cloned() {
            Command::new("tmux")
                .args(["split-window", "-h", "-t", first_pane])
                .status()?;
            if let Some(id) = get_pane_id(&dashboard_target) {
                pane_ids.push(id.clone());
                Command::new("tmux")
                    .args(["send-keys", "-t", &id, &capture_script(&names[1]), "Enter"])
                    .status()?;
            }
        }
    }

    // Split first pane vertically for agent 2
    if names.len() > 2 {
        if let Some(ref first_pane) = pane_ids.first().cloned() {
            Command::new("tmux")
                .args(["split-window", "-v", "-t", first_pane])
                .status()?;
            if let Some(id) = get_pane_id(&dashboard_target) {
                pane_ids.push(id.clone());
                Command::new("tmux")
                    .args(["send-keys", "-t", &id, &capture_script(&names[2]), "Enter"])
                    .status()?;
            }
        }
    }

    // Split second pane vertically for agent 3
    if names.len() > 3 {
        if let Some(ref second_pane) = pane_ids.get(1).cloned() {
            Command::new("tmux")
                .args(["split-window", "-v", "-t", second_pane])
                .status()?;
            if let Some(id) = get_pane_id(&dashboard_target) {
                pane_ids.push(id.clone());
                Command::new("tmux")
                    .args(["send-keys", "-t", &id, &capture_script(&names[3]), "Enter"])
                    .status()?;
            }
        }
    }

    // Bottom input pane — split the full width at the bottom
    Command::new("tmux")
        .args(["split-window", "-v", "-l", "3", "-t", &dashboard_target])
        .status()?;

    // Get the input pane ID (the newly created pane after the last split)
    let input_pane = get_pane_id(&dashboard_target)
        .unwrap_or_else(|| format!("{}.{}", dashboard_target, pane_ids.len()));
    Command::new("tmux")
        .args(["send-keys", "-t", &input_pane, &input_script, "Enter"])
        .status()?;

    // Focus the input pane
    Command::new("tmux")
        .args(["select-pane", "-t", &input_pane])
        .status()?;

    // Attach to the session (select dashboard window first)
    Output::info("Dashboard opened. Ctrl-b d to detach.");

    Command::new("tmux")
        .args(["select-window", "-t", &dashboard_target])
        .status()?;

    attach_tmux_session(session)
}

#[cfg(unix)]
fn attach_tmux_session(session: &str) -> anyhow::Result<()> {
    use std::os::unix::process::CommandExt;

    let err = Command::new("tmux")
        .args(["attach-session", "-t", session])
        .exec();

    anyhow::bail!("Failed to attach to tmux session: {}", err)
}

#[cfg(not(unix))]
fn attach_tmux_session(session: &str) -> anyhow::Result<()> {
    let status = Command::new("tmux")
        .args(["attach-session", "-t", session])
        .status()?;

    if status.success() {
        Ok(())
    } else {
        anyhow::bail!(
            "Failed to attach to tmux session (exit code: {})",
            status
                .code()
                .map(|code| code.to_string())
                .unwrap_or_else(|| "unknown".to_string())
        )
    }
}

// ---------------------------------------------------------------------------
// Web dashboard
// ---------------------------------------------------------------------------

/// Launch the web dashboard by shelling out to `synapt dashboard`.
pub fn run_spawn_web(port: u16, no_open: bool, _quiet: bool) -> anyhow::Result<()> {
    let mut args = vec![
        "dashboard".to_string(),
        "--port".to_string(),
        port.to_string(),
    ];
    if no_open {
        args.push("--no-open".to_string());
    }

    let status = Command::new("synapt").args(&args).status().map_err(|e| {
        anyhow::anyhow!(
            "Failed to run synapt dashboard: {} (is synapt[dashboard] installed?)",
            e
        )
    })?;

    if !status.success() {
        anyhow::bail!("synapt dashboard exited with status {}", status);
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make_agent(model: &str, args: Vec<&str>) -> AgentConfig {
        AgentConfig {
            role: "test".into(),
            model: model.into(),
            tool: "claude".into(),
            worktree: "main".into(),
            startup_prompt: None,
            cmd: vec![],
            args: args.into_iter().map(String::from).collect(),
            channel: None,
            loop_interval: "5m".into(),
            heartbeat_interval: 60,
            timeout_threshold: 180,
            restart_policy: "always".into(),
            restart_delay: 5,
            max_restarts: 3,
            env: HashMap::new(),
        }
    }

    /// Model injection: --model is auto-injected from agent.model when not in args (#472)
    #[test]
    fn test_model_injected_when_absent() {
        let agent = make_agent("claude-opus-4-6", vec!["-n", "opus"]);
        let default_args: Vec<String> = vec![];
        let resolved_args: Vec<String> = agent.args.clone();

        let has_model_flag = resolved_args.iter().any(|a| a == "--model")
            || default_args.iter().any(|a| a == "--model");

        assert!(!has_model_flag);

        let model_inject: Vec<String> = if !has_model_flag && !agent.model.is_empty() {
            vec!["--model".into(), agent.model.clone()]
        } else {
            vec![]
        };

        assert_eq!(model_inject, vec!["--model", "claude-opus-4-6"]);
    }

    /// Model injection: --model is NOT injected when already in agent.args (#472)
    #[test]
    fn test_model_not_duplicated_when_in_args() {
        let agent = make_agent(
            "claude-opus-4-6",
            vec!["--model", "claude-opus-4-6", "-n", "opus"],
        );
        let default_args: Vec<String> = vec![];
        let resolved_args: Vec<String> = agent.args.clone();

        let has_model_flag = resolved_args.iter().any(|a| a == "--model")
            || default_args.iter().any(|a| a == "--model");

        assert!(has_model_flag);

        let model_inject: Vec<String> = if !has_model_flag && !agent.model.is_empty() {
            vec!["--model".into(), agent.model.clone()]
        } else {
            vec![]
        };

        assert!(model_inject.is_empty());
    }

    /// Model injection: --model in default_args also prevents injection (#472)
    #[test]
    fn test_model_not_duplicated_when_in_default_args() {
        let agent = make_agent("claude-opus-4-6", vec!["-n", "opus"]);
        let default_args: Vec<String> = vec!["--model".into(), "claude-sonnet-4-6".into()];
        let resolved_args: Vec<String> = agent.args.clone();

        let has_model_flag = resolved_args.iter().any(|a| a == "--model")
            || default_args.iter().any(|a| a == "--model");

        assert!(has_model_flag);
    }

    /// Model injection: empty model string does not inject --model
    #[test]
    fn test_empty_model_no_injection() {
        let agent = make_agent("", vec!["-n", "opus"]);
        let default_args: Vec<String> = vec![];
        let resolved_args: Vec<String> = agent.args.clone();

        let has_model_flag = resolved_args.iter().any(|a| a == "--model")
            || default_args.iter().any(|a| a == "--model");

        let model_inject: Vec<String> = if !has_model_flag && !agent.model.is_empty() {
            vec!["--model".into(), agent.model.clone()]
        } else {
            vec![]
        };

        assert!(model_inject.is_empty());
    }

    #[test]
    fn test_agent_window_options_keep_panes_visible() {
        let options = agent_window_tmux_options();
        assert!(options.contains(&("remain-on-exit", "on")));
    }

    #[test]
    fn test_agent_window_options_raise_history_limit_for_dashboard_scrollback() {
        let options = agent_window_tmux_options();
        assert!(options.contains(&("history-limit", AGENT_HISTORY_LIMIT)));
        assert_eq!(AGENT_HISTORY_LIMIT, "50000");
    }

    // -- Resume detection tests (#579) ------------------------------------

    /// has_claude_session returns false for nonexistent directory
    #[test]
    fn test_no_session_for_missing_dir() {
        let tmp = std::env::temp_dir().join("grip_test_no_session_579");
        let _ = std::fs::remove_dir_all(&tmp);
        assert!(!has_claude_session(&tmp));
    }

    /// has_claude_session returns true when .jsonl files exist
    #[test]
    fn test_session_detected_with_jsonl() {
        let home = match std::env::var("HOME") {
            Ok(h) => h,
            Err(_) => return, // HOME not set (e.g. Windows); production code returns false
        };
        let tmp = tempfile::tempdir().unwrap();
        let worktree = tmp.path().join("agent-worktree");
        std::fs::create_dir_all(&worktree).unwrap();

        let abs = worktree.canonicalize().unwrap();
        let slug = abs.display().to_string().replace('/', "-");
        let session_dir = PathBuf::from(home)
            .join(".claude")
            .join("projects")
            .join(&slug);
        std::fs::create_dir_all(&session_dir).unwrap();
        std::fs::write(session_dir.join("abc123.jsonl"), "{}").unwrap();

        assert!(has_claude_session(&worktree));

        // Cleanup
        let _ = std::fs::remove_dir_all(&session_dir);
    }

    /// has_claude_session returns false when directory exists but no .jsonl
    #[test]
    fn test_no_session_without_jsonl() {
        let home = match std::env::var("HOME") {
            Ok(h) => h,
            Err(_) => return,
        };
        let tmp = tempfile::tempdir().unwrap();
        let worktree = tmp.path().join("agent-no-jsonl");
        std::fs::create_dir_all(&worktree).unwrap();

        let abs = worktree.canonicalize().unwrap();
        let slug = abs.display().to_string().replace('/', "-");
        let session_dir = PathBuf::from(home)
            .join(".claude")
            .join("projects")
            .join(&slug);
        std::fs::create_dir_all(&session_dir).unwrap();
        // Only a non-jsonl file
        std::fs::write(session_dir.join("notes.txt"), "hello").unwrap();

        assert!(!has_claude_session(&worktree));

        let _ = std::fs::remove_dir_all(&session_dir);
    }

    /// --resume is stripped from default_args when no session exists (#579)
    #[test]
    fn test_resume_stripped_when_no_session() {
        let defaults: Vec<String> = vec![
            "--resume".into(),
            "--permission-mode".into(),
            "bypassPermissions".into(),
        ];
        let worktree = std::env::temp_dir().join("grip_test_strip_resume_579");
        let _ = std::fs::remove_dir_all(&worktree);

        let has_resume = defaults.iter().any(|a| a == "--resume");
        assert!(has_resume);
        assert!(!has_claude_session(&worktree));

        let filtered: Vec<String> = defaults.into_iter().filter(|a| a != "--resume").collect();

        assert_eq!(filtered, vec!["--permission-mode", "bypassPermissions"]);
    }

    /// --resume is kept in default_args when session exists (#579)
    #[test]
    fn test_resume_kept_when_session_exists() {
        let home = match std::env::var("HOME") {
            Ok(h) => h,
            Err(_) => return,
        };
        let tmp = tempfile::tempdir().unwrap();
        let worktree = tmp.path().join("agent-with-session");
        std::fs::create_dir_all(&worktree).unwrap();

        let abs = worktree.canonicalize().unwrap();
        let slug = abs.display().to_string().replace('/', "-");
        let session_dir = PathBuf::from(home)
            .join(".claude")
            .join("projects")
            .join(&slug);
        std::fs::create_dir_all(&session_dir).unwrap();
        std::fs::write(session_dir.join("session.jsonl"), "{}").unwrap();

        let defaults: Vec<String> = vec![
            "--resume".into(),
            "--permission-mode".into(),
            "bypassPermissions".into(),
        ];

        assert!(has_claude_session(&worktree));

        // When session exists, --resume should be kept (no filtering)
        let kept: Vec<String> = defaults.clone();
        assert!(kept.contains(&"--resume".to_string()));

        let _ = std::fs::remove_dir_all(&session_dir);
    }
}
