//! Spawn command implementation
//!
//! Multi-agent orchestration: reads an agents.toml config file and
//! launches AI agents in tmux windows.

use crate::cli::output::Output;
use colored::Colorize;
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const AGENT_HISTORY_LIMIT: &str = "50000";
const CODEX_STARTUP_MAX_LINE_CHARS: usize = 500;
const LAUNCH_VERIFY_TIMEOUT_SECS: u64 = 8;
const LAUNCH_VERIFY_POLL_MS: u64 = 250;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PaneState {
    Running,
    Exited,
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LaunchTimeoutKind {
    CodexSelfUpdate,
    PendingOrShell,
}

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
    /// Opaque recipient-owned store coordinate published to the routing file.
    /// Defaults to `org_id`, then `session_name`.
    #[serde(default)]
    pub store_coordinate: Option<String>,
}

#[derive(Deserialize, Clone)]
pub struct ToolConfig {
    pub binary: String,
    #[serde(default)]
    pub cmd: Vec<String>,
    /// Tool-level launch args, composed BEFORE the agent's own `args` so an
    /// agent can override. The canonical key is `args`; `default_args` is
    /// accepted as a backward-compatible alias. The tool-level `args` key was
    /// silently dropped before this alias existed, so a launch flag set only at
    /// the tool level never reached the process.
    #[serde(default, alias = "default_args")]
    pub args: Vec<String>,
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

/// Whether `tmux -V` runs successfully. Unlike [`require_tmux`], returns a bool
/// so the caller can fall back to a foreground launch instead of refusing.
fn tmux_available() -> bool {
    Command::new("tmux")
        .arg("-V")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
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

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct AgentRoutingRecord {
    gripspace: String,
    qualified_alias: String,
    agent_id: String,
    store_coordinate: String,
    target: String,
    runtime: String,
}

fn routing_file_path() -> Option<PathBuf> {
    std::env::var_os("SYNAPT_AGENT_PANES_FILE")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn build_routing_records(
    config: &SpawnConfig,
    agent_ids: &HashMap<String, String>,
) -> anyhow::Result<Vec<AgentRoutingRecord>> {
    let gripspace = config
        .spawn
        .org_id
        .as_deref()
        .unwrap_or(&config.spawn.session_name);
    let store_coordinate = config
        .spawn
        .store_coordinate
        .as_deref()
        .unwrap_or(gripspace);
    sorted_agent_names(&config.agents)
        .into_iter()
        .map(|name| {
            let agent_id = agent_ids.get(&name).ok_or_else(|| {
                anyhow::anyhow!("stable agent ID was not resolved for configured agent {name}")
            })?;
            Ok(AgentRoutingRecord {
                gripspace: gripspace.to_string(),
                qualified_alias: format!("{}:{}", gripspace, name),
                agent_id: agent_id.clone(),
                store_coordinate: store_coordinate.to_string(),
                target: format!("{}:{}", config.spawn.session_name, name),
                runtime: config.agents[&name].tool.clone(),
            })
        })
        .collect()
}

fn routing_entry_gripspace(value: &Value) -> Option<&str> {
    value.get("gripspace").and_then(Value::as_str).or_else(|| {
        value
            .get("target")
            .and_then(Value::as_str)
            .and_then(|target| target.split_once(':').map(|(owner, _)| owner))
    })
}

fn qualified_alias_from_entry(key: &str, value: &Value) -> Option<String> {
    if key.contains(':') {
        return Some(key.to_string());
    }
    value
        .get("qualified_alias")
        .and_then(Value::as_str)
        .or_else(|| value.get("target").and_then(Value::as_str))
        .filter(|alias| alias.contains(':'))
        .map(str::to_string)
}

fn merge_routing_records(
    existing: Value,
    gripspace: &str,
    records: &[AgentRoutingRecord],
) -> anyhow::Result<Value> {
    let existing = existing
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("routing file must contain a JSON object"))?;
    let mut qualified = serde_json::Map::new();

    for (key, value) in existing {
        if routing_entry_gripspace(value) == Some(gripspace) {
            continue;
        }
        if let Some(alias) = qualified_alias_from_entry(key, value) {
            qualified.entry(alias).or_insert_with(|| value.clone());
        } else {
            qualified.insert(key.clone(), value.clone());
        }
    }

    for record in records {
        qualified.insert(
            record.qualified_alias.clone(),
            serde_json::to_value(record)?,
        );
    }

    let mut bare_counts: HashMap<String, usize> = HashMap::new();
    for key in qualified.keys().filter(|key| key.contains(':')) {
        let bare = key.split_once(':').expect("checked above").1.to_string();
        *bare_counts.entry(bare).or_default() += 1;
    }

    let aliases: Vec<(String, Value)> = qualified
        .iter()
        .filter_map(|(key, value)| {
            let (_, bare) = key.split_once(':')?;
            (bare_counts.get(bare) == Some(&1)).then(|| (bare.to_string(), value.clone()))
        })
        .collect();
    for (bare, value) in aliases {
        qualified.insert(bare, value);
    }

    Ok(Value::Object(qualified))
}

fn atomic_upsert_routing_file(
    path: &Path,
    gripspace: &str,
    records: &[AgentRoutingRecord],
) -> anyhow::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        anyhow::anyhow!("routing file has no parent directory: {}", path.display())
    })?;
    std::fs::create_dir_all(parent)?;

    let lock_path = parent.join(format!(
        ".{}.lock",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("agent-routing")
    ));
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)?;
    lock.lock_exclusive()?;

    let existing = match std::fs::read_to_string(path) {
        Ok(raw) => serde_json::from_str(&raw)
            .map_err(|error| anyhow::anyhow!("failed to parse {}: {}", path.display(), error))?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Value::Object(serde_json::Map::new())
        }
        Err(error) => return Err(error.into()),
    };
    let merged = merge_routing_records(existing, gripspace, records)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(&mut temporary, &merged)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    if let Ok(directory) = OpenOptions::new().read(true).open(parent) {
        let _ = directory.sync_all();
    }
    FileExt::unlock(&lock)?;
    Ok(())
}

fn agent_window_tmux_options() -> [(&'static str, &'static str); 2] {
    [
        ("remain-on-exit", "on"),
        ("history-limit", AGENT_HISTORY_LIMIT),
    ]
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn shell_join(parts: &[String]) -> String {
    parts
        .iter()
        .map(|part| shell_quote(part))
        .collect::<Vec<_>>()
        .join(" ")
}

fn build_launch_script_content(
    env: &HashMap<String, String>,
    worktree_path: &Path,
    launch_cmd: &str,
) -> String {
    let mut keys: Vec<&String> = env.keys().collect();
    keys.sort();

    let mut lines = vec!["#!/usr/bin/env bash".to_string(), "set -e".to_string()];
    for key in keys {
        if let Some(value) = env.get(key) {
            lines.push(format!("export {}={}", key, shell_quote(value)));
        }
    }
    lines.push(format!(
        "cd {}",
        shell_quote(&worktree_path.display().to_string())
    ));
    lines.push(format!("exec {}", launch_cmd));
    lines.push(String::new());
    lines.join("\n")
}

fn write_launch_script(
    workspace_root: &Path,
    agent_name: &str,
    env: &HashMap<String, String>,
    worktree_path: &Path,
    launch_cmd: &str,
) -> anyhow::Result<PathBuf> {
    let script_dir = workspace_root.join(".gitgrip").join("spawn");
    std::fs::create_dir_all(&script_dir)?;
    let safe_name = agent_name.replace(['/', '\\', ':'], "-");
    let script_path = script_dir.join(format!("{}-launch.sh", safe_name));
    let content = build_launch_script_content(env, worktree_path, launch_cmd);

    let mut file = tempfile::NamedTempFile::new_in(&script_dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(content.as_bytes())?;
    file.as_file().sync_all()?;
    file.persist(&script_path).map_err(|e| e.error)?;
    Ok(script_path)
}

fn tmux_send_launch_script(target: &str, script_path: &Path) -> anyhow::Result<()> {
    let command = format!("bash {}", shell_quote(&script_path.display().to_string()));
    let status = Command::new("tmux")
        .args(["send-keys", "-t", target, &command, "Enter"])
        .status()?;
    if !status.success() {
        anyhow::bail!("tmux send-keys launch exited with {}", status);
    }
    Ok(())
}

fn expected_process_name(binary: &str) -> String {
    Path::new(binary)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(binary)
        .to_string()
}

fn launch_process_matches(expected: &str, current: &str) -> bool {
    let expected = expected.to_ascii_lowercase();
    let current = current.trim().to_ascii_lowercase();
    if matches!(current.as_str(), "" | "sh" | "bash" | "zsh" | "fish") {
        return false;
    }
    if expected.contains("codex") {
        return current.contains("codex");
    }
    if expected.contains("claude") {
        // Claude Code's foreground process name can be version-like in tmux.
        // The failure class we need to reject is a returned shell prompt.
        return true;
    }
    current == expected || current.contains(&expected)
}

fn current_pane_command(target: &str) -> anyhow::Result<String> {
    let output = Command::new("tmux")
        .args([
            "display-message",
            "-t",
            target,
            "-p",
            "#{pane_current_command}",
        ])
        .output()?;
    if !output.status.success() {
        anyhow::bail!(
            "tmux display-message failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn capture_pane_tail(target: &str, lines: usize) -> String {
    let start = format!("-{}", lines);
    Command::new("tmux")
        .args(["capture-pane", "-t", target, "-p", "-S", &start])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).to_string())
        .unwrap_or_default()
}

fn wait_for_launch_process(
    target: &str,
    expected_process: &str,
    timeout: std::time::Duration,
) -> anyhow::Result<Option<String>> {
    let deadline = std::time::Instant::now() + timeout;
    let mut last = None;
    while std::time::Instant::now() < deadline {
        let current = current_pane_command(target)?;
        if launch_process_matches(expected_process, &current) {
            return Ok(Some(current));
        }
        last = Some(current);
        std::thread::sleep(std::time::Duration::from_millis(LAUNCH_VERIFY_POLL_MS));
    }
    Ok(last)
}

fn classify_launch_timeout(pane_tail: &str) -> LaunchTimeoutKind {
    if pane_tail.contains("Please restart Codex")
        || pane_tail.contains("Update ran successfully")
        || pane_tail.contains("restart Codex")
    {
        LaunchTimeoutKind::CodexSelfUpdate
    } else {
        LaunchTimeoutKind::PendingOrShell
    }
}

fn verify_launch_started(
    target: &str,
    expected_process: &str,
    script_path: &Path,
) -> anyhow::Result<()> {
    let timeout = std::time::Duration::from_secs(LAUNCH_VERIFY_TIMEOUT_SECS);
    if wait_for_launch_process(target, expected_process, timeout)?.is_some() {
        return Ok(());
    }

    let tail = capture_pane_tail(target, 30);
    match classify_launch_timeout(&tail) {
        LaunchTimeoutKind::CodexSelfUpdate => {
            Output::warning("Codex self-update exited during spawn; relaunching once");
            tmux_send_launch_script(target, script_path)?;
        }
        LaunchTimeoutKind::PendingOrShell => {
            Output::warning(
                "spawn launch did not reach agent process; clearing pane and retrying once",
            );
            let _ = Command::new("tmux")
                .args(["send-keys", "-t", target, "C-c"])
                .status();
            tmux_send_launch_script(target, script_path)?;
        }
    }

    if wait_for_launch_process(target, expected_process, timeout)?.is_some() {
        return Ok(());
    }

    let current = current_pane_command(target).unwrap_or_else(|_| "unknown".to_string());
    let tail = capture_pane_tail(target, 30);
    anyhow::bail!(
        "spawn launch for {} did not reach expected process '{}' (pane_current_command='{}'). Pane tail:\n{}",
        target,
        expected_process,
        current,
        tail.trim()
    )
}

// ---------------------------------------------------------------------------
// Subcommand: up
// ---------------------------------------------------------------------------

/// Build the launch inputs for one agent: its worktree path, the environment the
/// launch script exports, the exact command a pane would `exec`, and the process
/// name to verify (None in mock mode). Shared by the tmux path and the foreground
/// fallback so both run a byte-identical command.
#[allow(clippy::too_many_arguments)]
fn build_agent_launch(
    config: &SpawnConfig,
    workspace_root: &Path,
    agent: &AgentConfig,
    name: &str,
    agent_id: &str,
    channel: &str,
    mock_mode: bool,
) -> anyhow::Result<(PathBuf, HashMap<String, String>, String, Option<String>)> {
    let worktree_path = resolve_worktree_path(workspace_root, &agent.worktree);

    // Build environment variables for the launch script — GRIPSPACE_ROOT +
    // stable identity/recall namespace first, then config/env overrides.
    // The namespace describes the agent's desk, not whichever child
    // repository the runtime enters during a turn.
    let grip_root = workspace_root.display();
    let mut launch_env = HashMap::new();
    launch_env.insert("GRIPSPACE_ROOT".to_string(), grip_root.to_string());
    launch_env.insert("SYNAPT_AGENT_ID".to_string(), agent_id.to_string());
    launch_env.insert("SYNAPT_AGENT_NAME".to_string(), name.to_string());
    launch_env.insert(
        "SYNAPT_RECALL_WORKTREE".to_string(),
        worktree_path
            .file_name()
            .and_then(|part| part.to_str())
            .unwrap_or(name)
            .to_string(),
    );
    launch_env.insert("AGENT_NAME".to_string(), name.to_string());
    launch_env.insert("AGENT_ROLE".to_string(), agent.role.clone());
    launch_env.insert("SYNAPT_CHANNELS".to_string(), channel.to_string());
    launch_env.insert(
        "SYNAPT_LOOP_INTERVAL".to_string(),
        agent.loop_interval.clone(),
    );
    launch_env.extend(config.spawn.env.clone());
    launch_env.extend(agent.env.clone());

    let codex_startup_prompt = if !mock_mode && agent.tool == "codex" {
        read_agent_startup_prompt(workspace_root, agent).map_err(|e| {
            anyhow::anyhow!("failed to load Codex startup prompt for {}: {}", name, e)
        })?
    } else {
        String::new()
    };

    // Build the launch command
    let (launch_cmd, expected_process) = if mock_mode {
        let message = format!(
            "Agent {} would launch here (role: {}, model: {})",
            name, agent.role, agent.model
        );
        let inner = format!("printf '%s\\n' {}; exec sleep 86400", shell_quote(&message));
        (
            shell_join(&["bash".to_string(), "-lc".to_string(), inner]),
            None,
        )
    } else {
        // Resolve tool config
        let tool_config = config.tools.get(&agent.tool);
        let binary = tool_config
            .map(|t| t.binary.as_str())
            .unwrap_or(&agent.tool);

        // Build: binary + cmd + tool args + agent args
        // cmd: agent cmd overrides tool cmd (if agent has it)
        let cmd_parts: &[String] = if !agent.cmd.is_empty() {
            &agent.cmd
        } else {
            tool_config.map(|t| t.cmd.as_slice()).unwrap_or(&[])
        };

        // Tool-level args, composed before the agent's own args (agent overrides)
        let tool_args: &[String] = tool_config.map(|t| t.args.as_slice()).unwrap_or(&[]);

        // Resolve relative paths against gripspace root
        // (griptrees don't have .gitgrip/, so paths need to be absolute)
        let resolve = |arg: &str| -> String {
            if arg.starts_with(".gitgrip/") || arg.starts_with("prompts/") {
                workspace_root.join(arg).display().to_string()
            } else {
                arg.to_string()
            }
        };

        let resolved_defaults: Vec<String> = tool_args.iter().map(|s| resolve(s)).collect();
        let resolved_args: Vec<String> = agent.args.iter().map(|s| resolve(s)).collect();

        // Strip --resume when no prior session exists (#579)
        let has_resume = resolved_defaults.iter().any(|a| a == "--resume")
            || resolved_args.iter().any(|a| a == "--resume");
        let resolved_defaults: Vec<String> = if has_resume && !has_claude_session(&worktree_path) {
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

        let parts = assemble_launch_parts(
            binary,
            cmd_parts,
            &resolved_defaults,
            &model_inject,
            &resolved_args,
        );
        (
            finalize_launch_command(parts, &agent.tool, &codex_startup_prompt, &launch_env),
            Some(expected_process_name(binary)),
        )
    };

    Ok((worktree_path, launch_env, launch_cmd, expected_process))
}

/// Run one named agent in the foreground of the current terminal — the fallback
/// when no multiplexer is available (native Windows, a bare container) or when
/// `--interactive` is passed. Interactive mode is one agent only; a fleet with no
/// multiplexer refuses and names the single-agent form.
fn run_spawn_up_foreground(
    config: &SpawnConfig,
    workspace_root: &Path,
    targets: &[&str],
    agent_ids: &HashMap<String, String>,
    mock_mode: bool,
    tmux_ok: bool,
) -> anyhow::Result<()> {
    if targets.len() != 1 {
        anyhow::bail!(
            "interactive launch runs a single agent in this terminal{}.\n\
             Name one agent:\n    gr spawn up <agent> --interactive\n\
             Launching the whole fleet without a multiplexer is not yet supported.",
            if tmux_ok {
                ""
            } else {
                ", and tmux was not found"
            }
        );
    }

    let name = targets[0];
    let agent = &config.agents[name];
    let channel = agent.channel.as_deref().unwrap_or(&config.spawn.channel);
    let agent_id = agent_ids[name].clone();

    let (worktree_path, launch_env, launch_cmd, _expected_process) = build_agent_launch(
        config,
        workspace_root,
        agent,
        name,
        &agent_id,
        channel,
        mock_mode,
    )?;

    let launch_script = write_launch_script(
        workspace_root,
        name,
        &launch_env,
        &worktree_path,
        &launch_cmd,
    )?;

    println!();
    Output::header(&format!(
        "Launching {} in this terminal (cwd {})...",
        name,
        worktree_path.display()
    ));
    println!();

    // The launch script exports the env, cds into the worktree and execs the
    // same command a pane would run; run it in the foreground and inherit stdio.
    let status = Command::new("bash").arg(&launch_script).status()?;
    if !status.success() {
        anyhow::bail!(
            "agent '{}' exited with {}",
            name,
            status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "a signal".to_string())
        );
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn run_spawn_up(
    agent_filter: Option<String>,
    config_path: Option<String>,
    force_mock: bool,
    interactive: bool,
    verbose: bool,
    _quiet: bool,
    _json: bool,
) -> anyhow::Result<()> {
    // When tmux is absent (native Windows, a bare container) or --interactive is
    // passed, fall back to a single-agent foreground launch instead of refusing.
    let tmux_ok = tmux_available();
    let interactive_mode = interactive || !tmux_ok;
    let (config, workspace_root) = load_config(config_path.as_deref())?;
    let session = &config.spawn.session_name;
    let mock_mode = force_mock || config.spawn.mock_launch;
    let gripspace = config
        .spawn
        .org_id
        .as_deref()
        .unwrap_or(&config.spawn.session_name);
    let org_dir = crate::core::agent_registry::org_dir(gripspace);
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
        None => names.iter().map(|name| name.as_str()).collect(),
    };
    let mut agent_ids = HashMap::new();
    for name in &names {
        let agent = &config.agents[name];
        let agent_id =
            match crate::core::agent_registry::get_agent_by_name(&org_dir, gripspace, name)? {
                Some(entry) => entry.agent_id,
                None => crate::core::agent_registry::register_agent(
                    &org_dir,
                    gripspace,
                    name,
                    Some(&agent.role),
                )?,
            };
        agent_ids.insert(name.clone(), agent_id);
    }

    if let Some(path) = routing_file_path() {
        let records = build_routing_records(&config, &agent_ids)?;
        atomic_upsert_routing_file(&path, gripspace, &records).map_err(|error| {
            anyhow::anyhow!(
                "failed to refresh configured agent routing file {}: {}",
                path.display(),
                error
            )
        })?;
    }

    // Interactive / no-multiplexer fallback: run one named agent in the
    // foreground of this terminal. Everything above (id registration, routing
    // file) has already run, so the foreground agent has the same identity a
    // pane would.
    if interactive_mode {
        return run_spawn_up_foreground(
            &config,
            &workspace_root,
            &targets,
            &agent_ids,
            mock_mode,
            tmux_ok,
        );
    }
    // The tmux path. Keep the original guard so a no-tmux box that reached here
    // (only possible if tmux disappeared mid-run) gets a clear message.
    require_tmux()?;

    // Ensure tmux session exists
    if !session_exists(session) {
        create_session(session)?;
        Output::info(&format!("Created tmux session '{}'", session));
    }

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

        // Stable IDs were resolved before any pane or routing-file mutation.
        let agent_id = agent_ids[*name].clone();

        let (worktree_path, launch_env, launch_cmd, expected_process) = build_agent_launch(
            &config,
            &workspace_root,
            agent,
            name,
            &agent_id,
            channel,
            mock_mode,
        )?;

        if verbose {
            Output::info(&format!("  {name} launch command: {launch_cmd}"));
        }

        let launch_script = write_launch_script(
            &workspace_root,
            name,
            &launch_env,
            &worktree_path,
            &launch_cmd,
        )?;
        tmux_send_launch_script(&target, &launch_script)?;
        if let Some(expected_process) = expected_process.as_deref() {
            verify_launch_started(&target, expected_process, &launch_script)?;
        }

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

        if !mock_mode && agent.tool == "codex" {
            let recall_context = if config.spawn.auto_journal {
                generate_synapt_startup_context(
                    &worktree_path,
                    name,
                    &agent_id,
                    &agent.role,
                    channel,
                    &agent.loop_interval,
                    &config.spawn.env,
                    &agent.env,
                )
                .unwrap_or_default()
            } else {
                String::new()
            };
            if let Some(prompt) = build_codex_initial_prompt(name, &recall_context) {
                if let Err(e) = send_codex_initial_prompt(&target, &prompt) {
                    Output::warning(&format!(
                        "Failed to inject Codex startup context for {}: {}",
                        name, e
                    ));
                }
            }
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

fn resolve_startup_prompt_path(workspace_root: &Path, prompt_path: &str) -> PathBuf {
    let path = Path::new(prompt_path);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        workspace_root.join(path)
    }
}

fn read_agent_startup_prompt(workspace_root: &Path, agent: &AgentConfig) -> anyhow::Result<String> {
    let Some(prompt_path) = agent.startup_prompt.as_deref() else {
        return Ok(String::new());
    };
    let path = resolve_startup_prompt_path(workspace_root, prompt_path);
    std::fs::read_to_string(&path)
        .map_err(|e| anyhow::anyhow!("failed to read {}: {}", path.display(), e))
}

fn codex_developer_instruction_args(startup_prompt: &str) -> Vec<String> {
    if startup_prompt.trim().is_empty() {
        return Vec::new();
    }

    let value = toml::Value::String(startup_prompt.to_string());
    vec![
        "--config".to_string(),
        format!("developer_instructions={value}"),
    ]
}

/// Carry the agent's stable identity across Codex's MCP process boundary.
///
/// Codex starts configured stdio MCP servers itself. `mcp_servers.<id>.env`
/// is the documented literal environment map for that child process. Keep the
/// bridge deliberately narrower than the outer launch environment so API keys
/// and other agent-local values never become Codex configuration arguments.
fn codex_synapt_mcp_env_args(launch_env: &HashMap<String, String>) -> Vec<String> {
    const KEYS: &[&str] = &[
        "SYNAPT_AGENT_ID",
        "SYNAPT_AGENT_NAME",
        "SYNAPT_RECALL_ROOT",
        "SYNAPT_RECALL_WORKTREE",
    ];

    let mut args = Vec::new();
    for key in KEYS {
        let Some(value) = launch_env.get(*key).filter(|value| !value.is_empty()) else {
            continue;
        };
        args.push("--config".to_string());
        args.push(format!(
            "mcp_servers.synapt.env.{key}={}",
            toml::Value::String(value.clone())
        ));
    }
    args
}

/// Assemble the ordered launch argv, in order: `binary`, `cmd`, `tool_args`,
/// `model_inject`, `agent_args`. Tool-level args are composed BEFORE
/// agent-level args so an agent can override a tool default (last occurrence
/// wins downstream).
fn assemble_launch_parts(
    binary: &str,
    cmd_parts: &[String],
    tool_args: &[String],
    model_inject: &[String],
    agent_args: &[String],
) -> Vec<String> {
    let mut parts: Vec<String> = vec![binary.to_string()];
    parts.extend(cmd_parts.iter().cloned());
    parts.extend(tool_args.iter().cloned());
    parts.extend(model_inject.iter().cloned());
    parts.extend(agent_args.iter().cloned());
    parts
}

fn finalize_launch_command(
    mut parts: Vec<String>,
    agent_tool: &str,
    codex_startup_prompt: &str,
    launch_env: &HashMap<String, String>,
) -> String {
    parts.extend(codex_developer_instruction_args(codex_startup_prompt));
    if agent_tool == "codex" {
        parts.extend(codex_synapt_mcp_env_args(launch_env));
    }
    shell_join(&parts)
}

fn generate_synapt_startup_context(
    worktree_path: &Path,
    agent_name: &str,
    agent_id: &str,
    agent_role: &str,
    channel: &str,
    loop_interval: &str,
    global_env: &HashMap<String, String>,
    agent_env: &HashMap<String, String>,
) -> Option<String> {
    let mut cmd = Command::new("synapt");
    cmd.args(["recall", "startup", "--compact"])
        .current_dir(worktree_path)
        .env("AGENT_NAME", agent_name)
        .env("SYNAPT_AGENT_ID", agent_id)
        .env("AGENT_ROLE", agent_role)
        .env("SYNAPT_CHANNELS", channel)
        .env("SYNAPT_LOOP_INTERVAL", loop_interval);
    for (key, value) in global_env {
        cmd.env(key, value);
    }
    for (key, value) in agent_env {
        cmd.env(key, value);
    }

    let output = cmd.output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

fn build_codex_initial_prompt(agent_name: &str, recall_context: &str) -> Option<String> {
    let recall_context = recall_context.trim();
    if recall_context.is_empty() {
        return None;
    }

    let mut sections = vec![format!(
        "Load this startup context for agent `{}` before doing any work.",
        agent_name
    )];
    let recall_context = wrap_long_lines(recall_context, CODEX_STARTUP_MAX_LINE_CHARS);
    sections.push(format!(
        "<synapt_recall_startup_context>\n{}\n</synapt_recall_startup_context>",
        recall_context
    ));
    sections.push(
        "Use this context to choose the next action. Do not summarize it unless asked.".to_string(),
    );

    Some(sections.join("\n\n"))
}

fn wrap_long_lines(text: &str, max_chars: usize) -> String {
    let mut wrapped = Vec::new();
    for line in text.lines() {
        if line.chars().count() <= max_chars {
            wrapped.push(line.to_string());
            continue;
        }

        let mut current = String::new();
        let mut current_len = 0;
        for ch in line.chars() {
            if current_len >= max_chars {
                wrapped.push(current);
                current = String::new();
                current_len = 0;
            }
            current.push(ch);
            current_len += 1;
        }
        if !current.is_empty() {
            wrapped.push(current);
        }
    }
    wrapped.join("\n")
}

fn tmux_load_buffer(prompt: &str) -> anyhow::Result<()> {
    let mut child = Command::new("tmux")
        .args(["load-buffer", "-"])
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| anyhow::anyhow!("tmux load-buffer failed to start: {}", e))?;
    {
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow::anyhow!("tmux load-buffer stdin unavailable"))?;
        stdin.write_all(prompt.as_bytes())?;
    }
    let status = child.wait()?;
    if !status.success() {
        anyhow::bail!("tmux load-buffer exited with {}", status);
    }

    Ok(())
}

fn tmux_run(args: &[&str]) -> anyhow::Result<()> {
    let status = Command::new("tmux").args(args).status()?;
    if !status.success() {
        anyhow::bail!("tmux {} exited with {}", args.join(" "), status);
    }
    Ok(())
}

fn send_codex_initial_prompt_with<L, R, S>(
    target: &str,
    prompt: &str,
    mut load_buffer: L,
    mut run_tmux: R,
    mut sleep: S,
) -> anyhow::Result<()>
where
    L: FnMut(&str) -> anyhow::Result<()>,
    R: FnMut(&[&str]) -> anyhow::Result<()>,
    S: FnMut(std::time::Duration),
{
    load_buffer(prompt)?;
    run_tmux(&["paste-buffer", "-d", "-t", target])?;
    run_tmux(&["send-keys", "-t", target, "Enter"])?;

    sleep(std::time::Duration::from_millis(300));
    run_tmux(&["send-keys", "-t", target, "Enter"])?;

    sleep(std::time::Duration::from_millis(300));
    run_tmux(&["send-keys", "-t", target, "Enter"])?;

    Ok(())
}

fn send_codex_initial_prompt(target: &str, prompt: &str) -> anyhow::Result<()> {
    send_codex_initial_prompt_with(
        target,
        prompt,
        tmux_load_buffer,
        tmux_run,
        std::thread::sleep,
    )
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
        let final_state = match states[i] {
            PaneState::Running => match pane_exit_state(session, name) {
                Some(true) => PaneState::Exited,
                Some(false) => PaneState::Running,
                None => PaneState::Unknown,
            },
            other => other,
        };
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

        match final_state {
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

    fn routing_record(
        gripspace: &str,
        name: &str,
        agent_id: &str,
        runtime: &str,
    ) -> AgentRoutingRecord {
        AgentRoutingRecord {
            gripspace: gripspace.into(),
            qualified_alias: format!("{}:{}", gripspace, name),
            agent_id: agent_id.into(),
            store_coordinate: format!("store-{}", gripspace),
            target: format!("{}:{}", gripspace, name),
            runtime: runtime.into(),
        }
    }

    #[test]
    fn test_routing_upsert_replaces_owned_records_and_preserves_other_gripspaces() {
        let existing = serde_json::json!({
            "apollo": {"target": "synapt:apollo", "runtime": "claude"},
            "anchor": {"target": "conversa:anchor", "runtime": "claude"}
        });
        let merged = merge_routing_records(
            existing,
            "synapt",
            &[routing_record("synapt", "apollo", "apollo-001", "codex")],
        )
        .unwrap();

        assert_eq!(merged["synapt:apollo"]["runtime"], "codex");
        assert_eq!(merged["synapt:apollo"]["agent_id"], "apollo-001");
        assert_eq!(merged["synapt:apollo"]["store_coordinate"], "store-synapt");
        assert_eq!(merged["apollo"], merged["synapt:apollo"]);
        assert_eq!(merged["conversa:anchor"]["runtime"], "claude");
        assert_eq!(merged["anchor"], merged["conversa:anchor"]);
    }

    #[test]
    fn test_routing_upsert_rejects_ambiguous_bare_alias_by_omission() {
        let existing = serde_json::json!({
            "conversa:reviewer": {
                "gripspace": "conversa",
                "qualified_alias": "conversa:reviewer",
                "agent_id": "reviewer-002",
                "store_coordinate": "opaque-conversa",
                "target": "conversa:reviewer",
                "runtime": "claude"
            }
        });
        let merged = merge_routing_records(
            existing,
            "synapt",
            &[routing_record(
                "synapt",
                "reviewer",
                "reviewer-001",
                "codex",
            )],
        )
        .unwrap();

        assert!(merged.get("reviewer").is_none());
        assert!(merged.get("synapt:reviewer").is_some());
        assert!(merged.get("conversa:reviewer").is_some());
    }

    #[test]
    fn test_routing_file_concurrent_upserts_preserve_both_gripspaces() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("agent-panes.json");
        let first_path = path.clone();
        let second_path = path.clone();

        let first = std::thread::spawn(move || {
            atomic_upsert_routing_file(
                &first_path,
                "synapt",
                &[routing_record("synapt", "apollo", "apollo-001", "codex")],
            )
        });
        let second = std::thread::spawn(move || {
            atomic_upsert_routing_file(
                &second_path,
                "conversa",
                &[routing_record("conversa", "anchor", "anchor-001", "claude")],
            )
        });

        first.join().unwrap().unwrap();
        second.join().unwrap().unwrap();
        let value: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        assert_eq!(value["synapt:apollo"]["runtime"], "codex");
        assert_eq!(value["conversa:anchor"]["runtime"], "claude");
    }

    #[test]
    fn test_spawn_config_generates_runtime_identity_and_opaque_store_routing_record() {
        let mut agent = make_agent("gpt-5.6-sol", vec![]);
        agent.tool = "codex".into();
        let config = SpawnConfig {
            spawn: SpawnGlobal {
                session_name: "synapt-live".into(),
                channel: "dev".into(),
                auto_journal: false,
                mock_launch: false,
                env: HashMap::new(),
                org_id: Some("synapt".into()),
                store_coordinate: Some("opaque-coordinate-7".into()),
            },
            tools: HashMap::new(),
            agents: HashMap::from([("sentinel".into(), agent)]),
        };
        let ids = HashMap::from([("sentinel".into(), "sentinel-001".into())]);

        let records = build_routing_records(&config, &ids).unwrap();

        assert_eq!(
            records,
            vec![AgentRoutingRecord {
                gripspace: "synapt".into(),
                qualified_alias: "synapt:sentinel".into(),
                agent_id: "sentinel-001".into(),
                store_coordinate: "opaque-coordinate-7".into(),
                target: "synapt-live:sentinel".into(),
                runtime: "codex".into(),
            }]
        );
    }

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

    #[test]
    fn test_launch_process_match_accepts_codex_runtime_wrappers() {
        assert!(launch_process_matches(
            "codex",
            "codex-aarch64-apple-darwin"
        ));
        assert!(!launch_process_matches("codex", "zsh"));
    }

    #[test]
    fn test_launch_process_match_for_claude_rejects_shell_prompt() {
        assert!(launch_process_matches("claude", "2.1.198"));
        assert!(!launch_process_matches("claude", "zsh"));
    }

    #[test]
    fn test_launch_timeout_classifies_codex_self_update() {
        let tail = "Update ran successfully! Please restart Codex.";
        assert_eq!(
            classify_launch_timeout(tail),
            LaunchTimeoutKind::CodexSelfUpdate
        );
    }

    #[test]
    fn test_launch_script_quotes_env_and_execs_launch() {
        let mut env = HashMap::new();
        env.insert("AGENT_NAME".to_string(), "sentinel".to_string());
        env.insert("ODD".to_string(), "value with ' quote".to_string());

        let script = build_launch_script_content(
            &env,
            &PathBuf::from("/tmp/grip space"),
            "'codex' 'resume'",
        );

        assert!(script.contains("export AGENT_NAME='sentinel'"));
        assert!(script.contains("export ODD='value with '\\'' quote'"));
        assert!(script.contains("cd '/tmp/grip space'"));
        assert!(script.contains("exec 'codex' 'resume'"));
    }

    #[cfg(unix)]
    #[test]
    fn test_launch_script_is_owner_only() {
        use std::io::Read;
        use std::os::unix::fs::PermissionsExt;

        let workspace = tempfile::tempdir().unwrap();
        let script_dir = workspace.path().join(".gitgrip/spawn");
        std::fs::create_dir_all(&script_dir).unwrap();
        let existing_script = script_dir.join("sentinel-launch.sh");
        std::fs::write(&existing_script, "old public content").unwrap();
        std::fs::set_permissions(&existing_script, std::fs::Permissions::from_mode(0o644)).unwrap();
        let mut old_reader = std::fs::File::open(&existing_script).unwrap();

        let script_path = write_launch_script(
            workspace.path(),
            "sentinel",
            &HashMap::new(),
            Path::new("/tmp/worktree"),
            "'codex'",
        )
        .unwrap();

        let mode = std::fs::metadata(script_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        let mut old_content = String::new();
        old_reader.read_to_string(&mut old_content).unwrap();
        assert_eq!(old_content, "old public content");
        assert!(!std::fs::read_to_string(existing_script)
            .unwrap()
            .contains("old public content"));
    }

    #[test]
    fn test_codex_developer_instructions_preserve_startup_prompt() {
        let startup_prompt = concat!(
            "You are Stromus.\n",
            "Quotes: \"truth\" and 'care', plus \"\"\" and ''' fences.\n",
            "Path: C:\\work\\synapt\n",
            "Shell-like text stays text: $(touch nope) and `touch nope`."
        );
        let args = codex_developer_instruction_args(startup_prompt);

        assert_eq!(args[0], "--config");
        let parsed = args[1].parse::<toml::Table>().unwrap();
        assert_eq!(
            parsed
                .get("developer_instructions")
                .and_then(|v| v.as_str()),
            Some(startup_prompt)
        );
    }

    #[test]
    fn test_codex_developer_instructions_preserve_large_prompt() {
        let startup_prompt = format!("# Stromus\n\n{}", "identity substrate\n".repeat(4_000));
        let args = codex_developer_instruction_args(&startup_prompt);
        let parsed = args[1].parse::<toml::Table>().unwrap();

        assert_eq!(
            parsed
                .get("developer_instructions")
                .and_then(|v| v.as_str()),
            Some(startup_prompt.as_str())
        );
    }

    #[test]
    fn test_codex_developer_instructions_skip_empty_prompt() {
        assert!(codex_developer_instruction_args("").is_empty());
        assert!(codex_developer_instruction_args("  \n").is_empty());
    }

    fn synapt_env_value(override_arg: &str, key: &str) -> Option<String> {
        let parsed = override_arg.parse::<toml::Table>().unwrap();
        parsed
            .get("mcp_servers")?
            .get("synapt")?
            .get("env")?
            .get(key)?
            .as_str()
            .map(str::to_string)
    }

    #[test]
    fn test_codex_synapt_mcp_env_is_allowlisted_and_toml_quoted() {
        let special_name = concat!(
            "Sentinel's \"night\" watch\\desk\n",
            "$(touch must-not-run) `touch must-not-run-either` 🦉"
        );
        let env = HashMap::from([
            ("SYNAPT_AGENT_ID".to_string(), "sentinel-001".to_string()),
            ("SYNAPT_AGENT_NAME".to_string(), special_name.to_string()),
            (
                "SYNAPT_RECALL_ROOT".to_string(),
                "/tmp/recall root".to_string(),
            ),
            (
                "SYNAPT_RECALL_WORKTREE".to_string(),
                "synapt-global".to_string(),
            ),
            (
                "OPENAI_API_KEY".to_string(),
                "dummy-secret-that-must-not-cross".to_string(),
            ),
            ("EMPTY_ALLOWED_VALUE".to_string(), String::new()),
        ]);

        let args = codex_synapt_mcp_env_args(&env);

        assert_eq!(args.len(), 8);
        assert_eq!(args.iter().filter(|arg| *arg == "--config").count(), 4);
        assert_eq!(
            synapt_env_value(&args[1], "SYNAPT_AGENT_ID"),
            Some("sentinel-001".to_string())
        );
        assert_eq!(
            synapt_env_value(&args[3], "SYNAPT_AGENT_NAME"),
            Some(special_name.to_string())
        );
        assert_eq!(
            synapt_env_value(&args[5], "SYNAPT_RECALL_ROOT"),
            Some("/tmp/recall root".to_string())
        );
        assert_eq!(
            synapt_env_value(&args[7], "SYNAPT_RECALL_WORKTREE"),
            Some("synapt-global".to_string())
        );
        let rendered = args.join(" ");
        assert!(!rendered.contains("OPENAI_API_KEY"));
        assert!(!rendered.contains("dummy-secret-that-must-not-cross"));
        assert!(!rendered.contains("EMPTY_ALLOWED_VALUE"));
    }

    #[cfg(unix)]
    #[test]
    fn test_codex_synapt_mcp_override_survives_launch_shell_quoting() {
        let special_name = "Sentinel's \"night\" watch\\desk\n$(printf injected)";
        let env = HashMap::from([("SYNAPT_AGENT_NAME".to_string(), special_name.to_string())]);
        let args = codex_synapt_mcp_env_args(&env);
        let command = shell_join(&["printf".to_string(), "%s".to_string(), args[1].clone()]);

        let output = Command::new("sh").args(["-c", &command]).output().unwrap();

        assert!(output.status.success());
        let transported = String::from_utf8(output.stdout).unwrap();
        assert_eq!(transported, args[1]);
        assert_eq!(
            synapt_env_value(&transported, "SYNAPT_AGENT_NAME"),
            Some(special_name.to_string())
        );
    }

    #[test]
    fn test_generated_codex_launch_command_contains_synapt_mcp_identity_bridge() {
        let env = HashMap::from([
            ("SYNAPT_AGENT_ID".to_string(), "sentinel-001".to_string()),
            ("SYNAPT_AGENT_NAME".to_string(), "sentinel".to_string()),
            (
                "SYNAPT_RECALL_WORKTREE".to_string(),
                "synapt-global".to_string(),
            ),
        ]);
        let base = vec![
            "codex".to_string(),
            "resume".to_string(),
            "--last".to_string(),
        ];

        let command = finalize_launch_command(base, "codex", "You are Sentinel.", &env);

        assert_eq!(
            command,
            shell_join(&[
                "codex".to_string(),
                "resume".to_string(),
                "--last".to_string(),
                "--config".to_string(),
                "developer_instructions=\"You are Sentinel.\"".to_string(),
                "--config".to_string(),
                "mcp_servers.synapt.env.SYNAPT_AGENT_ID=\"sentinel-001\"".to_string(),
                "--config".to_string(),
                "mcp_servers.synapt.env.SYNAPT_AGENT_NAME=\"sentinel\"".to_string(),
                "--config".to_string(),
                "mcp_servers.synapt.env.SYNAPT_RECALL_WORKTREE=\"synapt-global\"".to_string(),
            ])
        );
    }

    #[test]
    fn test_non_codex_launch_command_never_receives_codex_mcp_overrides() {
        let env = HashMap::from([("SYNAPT_AGENT_ID".to_string(), "apollo-001".to_string())]);

        let command = finalize_launch_command(
            vec!["claude".to_string(), "--resume".to_string()],
            "claude",
            "",
            &env,
        );

        assert_eq!(command, "'claude' '--resume'");
        assert!(!command.contains("mcp_servers"));
    }

    #[test]
    fn test_codex_initial_prompt_contains_recall_context_only() {
        let prompt =
            build_codex_initial_prompt("opus", "Last session: shipped recall startup injection.")
                .unwrap();

        assert!(prompt.contains("agent `opus`"));
        assert!(!prompt.contains("<agent_startup_prompt>"));
        assert!(prompt.contains("<synapt_recall_startup_context>"));
        assert!(prompt.contains("Last session: shipped recall startup injection."));
        assert!(prompt.contains("Do not summarize it unless asked."));
    }

    #[test]
    fn test_codex_initial_prompt_skips_empty_context() {
        assert!(build_codex_initial_prompt("opus", "").is_none());

        let prompt = build_codex_initial_prompt("opus", "Recall context").unwrap();
        assert!(!prompt.contains("<agent_startup_prompt>"));
        assert!(prompt.contains("<synapt_recall_startup_context>"));
    }

    #[test]
    fn test_codex_initial_prompt_wraps_long_recall_lines() {
        let recall = "x".repeat(CODEX_STARTUP_MAX_LINE_CHARS + 1);
        let prompt = build_codex_initial_prompt("opus", &recall).unwrap();
        assert!(prompt.contains(&format!("{}\nx", "x".repeat(CODEX_STARTUP_MAX_LINE_CHARS))));
    }

    #[test]
    fn test_codex_prompt_transport_pastes_then_sends_exactly_three_enters() {
        let mut loaded = Vec::new();
        let mut commands = Vec::new();
        let mut sleeps = Vec::new();

        send_codex_initial_prompt_with(
            "synapt:atlas",
            "recall context",
            |prompt| {
                loaded.push(prompt.to_string());
                Ok(())
            },
            |args| {
                commands.push(args.iter().map(|arg| arg.to_string()).collect::<Vec<_>>());
                Ok(())
            },
            |duration| sleeps.push(duration),
        )
        .unwrap();

        assert_eq!(loaded, ["recall context"]);
        assert_eq!(
            commands,
            [
                ["paste-buffer", "-d", "-t", "synapt:atlas"],
                ["send-keys", "-t", "synapt:atlas", "Enter"],
                ["send-keys", "-t", "synapt:atlas", "Enter"],
                ["send-keys", "-t", "synapt:atlas", "Enter"],
            ]
        );
        assert_eq!(
            sleeps,
            [
                std::time::Duration::from_millis(300),
                std::time::Duration::from_millis(300),
            ]
        );
    }

    #[test]
    fn test_codex_prompt_transport_stops_after_tmux_failure() {
        let mut commands = Vec::new();
        let error = send_codex_initial_prompt_with(
            "synapt:sentinel",
            "recall context",
            |_| Ok(()),
            |args| {
                commands.push(args.iter().map(|arg| arg.to_string()).collect::<Vec<_>>());
                if commands.len() == 3 {
                    anyhow::bail!("simulated tmux failure");
                }
                Ok(())
            },
            |_| {},
        )
        .unwrap_err();

        assert_eq!(error.to_string(), "simulated tmux failure");
        assert_eq!(commands.len(), 3);
        assert_eq!(commands[2], ["send-keys", "-t", "synapt:sentinel", "Enter"]);
    }

    #[test]
    fn test_resolve_startup_prompt_path_uses_workspace_for_relative_paths() {
        let root = PathBuf::from("/tmp/gripspace");
        assert_eq!(
            resolve_startup_prompt_path(&root, ".gitgrip/prompts/opus.md"),
            PathBuf::from("/tmp/gripspace/.gitgrip/prompts/opus.md")
        );
        assert_eq!(
            resolve_startup_prompt_path(&root, "/abs/prompt.md"),
            PathBuf::from("/abs/prompt.md")
        );
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

    // A tool-level `args` key must reach the ToolConfig. Before the fix the
    // field was named `default_args`, so `[tools.<t>] args = [...]` was an
    // unknown key and dropped silently: a launch flag set only at the tool
    // level (e.g. a hook-trust bypass) never reached the spawned process.
    #[test]
    fn tool_level_args_key_is_deserialized() {
        let toml = r#"
[spawn]
[tools.mock]
binary = "mockbin"
args = ["--tool-flag"]
[agents.x]
role = "worker"
tool = "mock"
"#;
        let cfg: SpawnConfig = toml::from_str(toml).unwrap();
        assert_eq!(
            cfg.tools["mock"].args,
            vec!["--tool-flag".to_string()],
            "tool-level `args` key must populate ToolConfig.args"
        );
    }

    // The old key stays valid through the serde alias.
    #[test]
    fn tool_level_default_args_alias_still_works() {
        let toml = r#"
[spawn]
[tools.mock]
binary = "mockbin"
default_args = ["--legacy-flag"]
[agents.x]
role = "worker"
tool = "mock"
"#;
        let cfg: SpawnConfig = toml::from_str(toml).unwrap();
        assert_eq!(
            cfg.tools["mock"].args,
            vec!["--legacy-flag".to_string()],
            "`default_args` alias must still populate ToolConfig.args"
        );
    }

    // Composition order: binary, cmd, TOOL args, model, AGENT args — tool-level
    // args precede agent-level args so an agent can override a tool default.
    #[test]
    fn launch_parts_compose_tool_args_before_agent_args() {
        let parts = assemble_launch_parts(
            "mockbin",
            &["exec".to_string()],
            &["--tool-flag".to_string()],
            &[],
            &["--agent-flag".to_string()],
        );
        assert_eq!(
            parts,
            vec![
                "mockbin".to_string(),
                "exec".to_string(),
                "--tool-flag".to_string(),
                "--agent-flag".to_string(),
            ],
        );
        // The tool flag must appear strictly before the agent flag.
        let ti = parts.iter().position(|a| a == "--tool-flag").unwrap();
        let ai = parts.iter().position(|a| a == "--agent-flag").unwrap();
        assert!(ti < ai, "tool-level args must precede agent-level args");
    }
}
