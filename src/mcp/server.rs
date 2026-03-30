//! stdio MCP server for gitgrip operations.

use anyhow::Context;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::Duration;

const MCP_PROTOCOL_VERSION: &str = "2024-11-05";
const DEFAULT_MAX_CAPTURE_BYTES: usize = 1024 * 1024;
const MAX_CAPTURE_ENV: &str = "GITGRIP_MCP_MAX_CAPTURE_BYTES";
const CAPTURE_TRUNCATED_MSG: &str = "\n[output truncated: exceeded capture limit]";

#[derive(Debug, Deserialize)]
struct RpcRequest {
    #[allow(dead_code)]
    jsonrpc: Option<String>,
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Value,
}

#[derive(Debug, Deserialize)]
struct ToolCallParams {
    name: String,
    #[serde(default)]
    arguments: Value,
}

#[derive(Debug, Deserialize)]
struct CancelledParams {
    #[serde(rename = "requestId")]
    request_id: Value,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct RepoArgs {
    repo: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct GenerateContextArgs {
    #[serde(default)]
    dry_run: bool,
}

struct CommandOutput {
    success: bool,
    cancelled: bool,
    status_code: Option<i32>,
    stdout: String,
    stderr: String,
}

struct ContextCommandOutput {
    success: bool,
    cancelled: bool,
    status_code: Option<i32>,
    context_json: Option<Value>,
    stderr: String,
}

enum ReaderEvent {
    Frame(Vec<u8>),
    Eof,
    Error(String),
}

struct WorkerResponse {
    request_key: String,
    response: Value,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct PassthroughArgs {
    #[serde(default)]
    args: Vec<String>,
}

#[derive(Clone, Copy)]
struct CliToolSpec {
    tool_name: &'static str,
    command: &'static str,
    description: &'static str,
}

const CLI_TOOL_SPECS: &[CliToolSpec] = &[
    CliToolSpec {
        tool_name: "gitgrip_init",
        command: "init",
        description: "Initialize a new workspace (`gr init ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_sync",
        command: "sync",
        description: "Sync all repositories (`gr sync ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_status",
        command: "status",
        description: "Show status of all repositories (`gr status ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_branch",
        command: "branch",
        description: "Create/switch branches across repos (`gr branch ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_checkout",
        command: "checkout",
        description: "Checkout a branch across repos (`gr checkout ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_add",
        command: "add",
        description: "Stage changes across repos (`gr add ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_diff",
        command: "diff",
        description: "Show diff across repos (`gr diff ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_commit",
        command: "commit",
        description: "Commit changes across repos (`gr commit ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_push",
        command: "push",
        description: "Push changes across repos (`gr push ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_prune",
        command: "prune",
        description: "Clean merged branches across repos (`gr prune ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_pr",
        command: "pr",
        description: "Pull request operations (`gr pr ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_tree",
        command: "tree",
        description: "Griptree operations (`gr tree ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_grep",
        command: "grep",
        description: "Search across repos (`gr grep ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_forall",
        command: "forall",
        description: "Run a command in each repo (`gr forall ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_rebase",
        command: "rebase",
        description: "Rebase branches across repos (`gr rebase ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_pull",
        command: "pull",
        description: "Pull latest changes across repos (`gr pull ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_link",
        command: "link",
        description: "Manage links (`gr link ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_run",
        command: "run",
        description: "Run workspace scripts (`gr run ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_env",
        command: "env",
        description: "Show environment variables (`gr env`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_bench",
        command: "bench",
        description: "Run benchmarks (`gr bench ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_repo",
        command: "repo",
        description: "Repository operations (`gr repo ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_group",
        command: "group",
        description: "Repository group operations (`gr group ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_gc",
        command: "gc",
        description: "Run garbage collection (`gr gc ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_cherry_pick",
        command: "cherry-pick",
        description: "Cherry-pick commits across repos (`gr cherry-pick ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_issue",
        command: "issue",
        description: "Issue operations — list, create, view, close, reopen (`gr issue ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_ci",
        command: "ci",
        description: "CI/CD pipeline operations (`gr ci ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_manifest",
        command: "manifest",
        description: "Manifest operations (`gr manifest ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_agent",
        command: "agent",
        description: "Agent operations (`gr agent ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_release",
        command: "release",
        description: "Automated release workflow (`gr release ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_completions",
        command: "completions",
        description: "Generate shell completions (`gr completions ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_target",
        command: "target",
        description: "View or set PR target branch (`gr target ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_verify",
        command: "verify",
        description: "Verify workspace assertions (`gr verify ...`).",
    },
    CliToolSpec {
        tool_name: "gitgrip_restore",
        command: "restore",
        description: "Restore/unstage files across repos (`gr restore ...`).",
    },
];

/// Run the gitgrip MCP server over stdio.
pub fn run_mcp_server() -> anyhow::Result<()> {
    let (reader_tx, reader_rx) = mpsc::channel::<ReaderEvent>();
    thread::spawn(move || {
        let stdin = io::stdin();
        let mut reader = BufReader::new(stdin.lock());

        loop {
            match read_frame(&mut reader) {
                Ok(Some(bytes)) => {
                    if reader_tx.send(ReaderEvent::Frame(bytes)).is_err() {
                        break;
                    }
                }
                Ok(None) => {
                    let _ = reader_tx.send(ReaderEvent::Eof);
                    break;
                }
                Err(err) => {
                    let _ = reader_tx.send(ReaderEvent::Error(err.to_string()));
                    break;
                }
            }
        }
    });

    let (worker_tx, worker_rx) = mpsc::channel::<WorkerResponse>();
    let stdout = io::stdout();
    let mut writer = io::BufWriter::new(stdout.lock());

    let mut reader_done = false;
    let mut cancellation_map: HashMap<String, Arc<AtomicBool>> = HashMap::new();

    loop {
        while let Ok(done) = worker_rx.try_recv() {
            cancellation_map.remove(&done.request_key);
            write_frame(&mut writer, &done.response)?;
        }

        if reader_done && cancellation_map.is_empty() {
            break;
        }

        match reader_rx.recv_timeout(Duration::from_millis(50)) {
            Ok(ReaderEvent::Frame(bytes)) => {
                let request: RpcRequest = match serde_json::from_slice(&bytes) {
                    Ok(req) => req,
                    Err(err) => {
                        let response =
                            jsonrpc_error(Value::Null, -32700, &format!("Parse error: {err}"));
                        write_frame(&mut writer, &response)?;
                        continue;
                    }
                };

                if request.method == "notifications/cancelled" {
                    handle_cancel_notification(request.params, &cancellation_map);
                    continue;
                }

                let Some(id) = request.id.clone() else {
                    continue;
                };

                if request.method == "tools/call" {
                    let request_key = request_id_key(&id);
                    let cancel_flag = Arc::new(AtomicBool::new(false));
                    cancellation_map.insert(request_key.clone(), Arc::clone(&cancel_flag));

                    let tx = worker_tx.clone();
                    let params = request.params;
                    thread::spawn(move || {
                        let response = handle_tool_call(id, params, Some(cancel_flag));
                        let _ = tx.send(WorkerResponse {
                            request_key,
                            response,
                        });
                    });
                    continue;
                }

                if let Some(response) = handle_request(request) {
                    write_frame(&mut writer, &response)?;
                }
            }
            Ok(ReaderEvent::Eof) => {
                reader_done = true;
            }
            Ok(ReaderEvent::Error(message)) => {
                let response = jsonrpc_error(
                    Value::Null,
                    -32000,
                    &format!("MCP server read failure: {message}"),
                );
                write_frame(&mut writer, &response)?;
                reader_done = true;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                reader_done = true;
            }
        }
    }

    Ok(())
}

fn handle_request(request: RpcRequest) -> Option<Value> {
    let id = request.id?;

    match request.method.as_str() {
        "initialize" => {
            let result = json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {
                        "listChanged": false
                    },
                    "resources": {
                        "subscribe": false,
                        "listChanged": false
                    }
                },
                "serverInfo": {
                    "name": "gitgrip",
                    "version": env!("CARGO_PKG_VERSION")
                }
            });
            Some(jsonrpc_result(id, result))
        }
        "ping" => Some(jsonrpc_result(id, json!({}))),
        "tools/list" => {
            let result = json!({
                "tools": tools_definition()
            });
            Some(jsonrpc_result(id, result))
        }
        "resources/list" => {
            let result = json!({
                "resources": resources_definition()
            });
            Some(jsonrpc_result(id, result))
        }
        "resources/templates/list" => {
            let result = json!({
                "resourceTemplates": []
            });
            Some(jsonrpc_result(id, result))
        }
        "resources/read" => {
            let Some(uri) = request.params.get("uri").and_then(|v| v.as_str()) else {
                return Some(jsonrpc_error(id, -32602, "Missing required parameter: uri"));
            };
            let result = handle_resource_read(uri);
            if result.get("error").is_some() {
                Some(jsonrpc_error(
                    id,
                    result["error"]["code"].as_i64().unwrap_or(-32002),
                    result["error"]["message"]
                        .as_str()
                        .unwrap_or("Unknown error"),
                ))
            } else {
                Some(jsonrpc_result(id, result))
            }
        }
        _ => Some(jsonrpc_error(
            id,
            -32601,
            &format!("Method not found: {}", request.method),
        )),
    }
}

fn handle_cancel_notification(params: Value, cancellation_map: &HashMap<String, Arc<AtomicBool>>) {
    let Ok(parsed) = serde_json::from_value::<CancelledParams>(params) else {
        return;
    };

    let key = request_id_key(&parsed.request_id);
    if let Some(flag) = cancellation_map.get(&key) {
        flag.store(true, Ordering::SeqCst);
    }
}

fn request_id_key(id: &Value) -> String {
    id.to_string()
}

fn handle_tool_call(id: Value, params: Value, cancel_flag: Option<Arc<AtomicBool>>) -> Value {
    let parsed: ToolCallParams = match serde_json::from_value(params) {
        Ok(v) => v,
        Err(err) => {
            return jsonrpc_error(id, -32602, &format!("Invalid params for tools/call: {err}"));
        }
    };

    let tool_result: anyhow::Result<Value> = (|| match parsed.name.as_str() {
        "gitgrip_agent_context" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_context_tool(args.repo, cancel_flag)
        }
        "gitgrip_agent_build" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("agent build", args.repo, &["agent", "build"], cancel_flag)
        }
        "gitgrip_agent_test" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("agent test", args.repo, &["agent", "test"], cancel_flag)
        }
        "gitgrip_agent_verify" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("agent verify", args.repo, &["agent", "verify"], cancel_flag)
        }
        "gitgrip_agent_generate_context" => {
            let args: GenerateContextArgs = parse_tool_args(parsed.arguments)?;
            let mut cmd = vec!["agent".to_string(), "generate-context".to_string()];
            if args.dry_run {
                cmd.push("--dry-run".to_string());
            }
            run_command_tool("agent generate-context", &cmd, None, cancel_flag)
        }
        name => {
            let spec = find_cli_tool_spec(name)
                .ok_or_else(|| anyhow::anyhow!("Unknown tool: {}", parsed.name))?;
            let args: PassthroughArgs = parse_tool_args(parsed.arguments)?;
            run_passthrough_tool(spec, args, cancel_flag)
        }
    })();

    match tool_result {
        Ok(result) => jsonrpc_result(id, result),
        Err(err) => jsonrpc_result(
            id,
            tool_text_response(&err.to_string(), true, None, false, None),
        ),
    }
}

fn run_context_tool(
    repo: Option<String>,
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<Value> {
    let mut args = vec![
        "--json".to_string(),
        "agent".to_string(),
        "context".to_string(),
    ];
    if let Some(repo) = repo {
        args.push("--repo".to_string());
        args.push(repo);
    }

    let out = run_context_command(&args, cancel_flag)?;
    if !out.success {
        let text = command_failure_context("agent context", &out);
        return Ok(tool_text_response(
            &text,
            true,
            None,
            out.cancelled,
            out.status_code,
        ));
    }

    let parsed_json = out
        .context_json
        .context("agent context did not produce structured JSON output")?;

    let pretty = serde_json::to_string_pretty(&parsed_json)?;
    Ok(tool_text_response(
        &pretty,
        false,
        Some(parsed_json),
        false,
        out.status_code,
    ))
}

fn run_text_tool(
    label: &str,
    repo: Option<String>,
    base_args: &[&str],
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<Value> {
    let mut args: Vec<String> = base_args.iter().map(|s| s.to_string()).collect();
    if let Some(repo) = repo {
        args.push(repo);
    }
    run_command_tool(label, &args, None, cancel_flag)
}

fn run_command_tool(
    label: &str,
    args: &[String],
    structured: Option<Value>,
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<Value> {
    let out = run_gitgrip_command(args, cancel_flag)?;
    if out.success {
        let text = non_empty_output(&out.stdout, &out.stderr)
            .unwrap_or_else(|| format!("gitgrip {label} completed successfully"));
        Ok(tool_text_response(
            &text,
            false,
            structured,
            false,
            out.status_code,
        ))
    } else {
        let text = command_failure(label, &out);
        Ok(tool_text_response(
            &text,
            true,
            structured,
            out.cancelled,
            out.status_code,
        ))
    }
}

fn run_passthrough_tool(
    spec: &CliToolSpec,
    args: PassthroughArgs,
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<Value> {
    let mut cmd = vec![spec.command.to_string()];
    cmd.extend(args.args);
    run_command_tool(spec.command, &cmd, None, cancel_flag)
}

fn parse_tool_args<T: for<'de> Deserialize<'de>>(value: Value) -> anyhow::Result<T> {
    if value.is_null() {
        return serde_json::from_value(json!({})).context("Failed to parse default args");
    }
    serde_json::from_value(value).context("Invalid tool arguments")
}

fn run_gitgrip_command(
    args: &[String],
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<CommandOutput> {
    let exe = std::env::current_exe().context("Failed to locate current gitgrip executable")?;
    let mut child = Command::new(exe)
        .args(args)
        .env("NO_COLOR", "1")
        .env("CLICOLOR", "0")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("Failed to execute gitgrip subprocess")?;

    let stdout = child
        .stdout
        .take()
        .context("Failed to capture subprocess stdout")?;
    let stderr = child
        .stderr
        .take()
        .context("Failed to capture subprocess stderr")?;

    let max_capture = max_capture_bytes();
    let out_thread = thread::spawn(move || capture_stream(stdout, max_capture));
    let err_thread = thread::spawn(move || capture_stream(stderr, max_capture));

    let cancel_status = start_cancel_controller(child.id(), cancel_flag.clone());

    let status = child.wait().context("Failed waiting for subprocess")?;
    cancel_status.done.store(true, Ordering::SeqCst);
    let _ = cancel_status.join.join();
    let cancelled = cancel_status.kill_sent.load(Ordering::SeqCst);

    let (stdout, stdout_truncated) = out_thread
        .join()
        .map_err(|_| anyhow::anyhow!("stdout capture thread panicked"))??;
    let (stderr, stderr_truncated) = err_thread
        .join()
        .map_err(|_| anyhow::anyhow!("stderr capture thread panicked"))??;

    let mut stdout = String::from_utf8_lossy(&stdout).to_string();
    let mut stderr = String::from_utf8_lossy(&stderr).to_string();

    if stdout_truncated {
        stdout.push_str(CAPTURE_TRUNCATED_MSG);
    }
    if stderr_truncated {
        stderr.push_str(CAPTURE_TRUNCATED_MSG);
    }

    Ok(CommandOutput {
        success: status.success() && !cancelled,
        cancelled,
        status_code: status.code(),
        stdout,
        stderr,
    })
}

fn run_context_command(
    args: &[String],
    cancel_flag: Option<Arc<AtomicBool>>,
) -> anyhow::Result<ContextCommandOutput> {
    let exe = std::env::current_exe().context("Failed to locate current gitgrip executable")?;
    let mut child = Command::new(exe)
        .args(args)
        .env("NO_COLOR", "1")
        .env("CLICOLOR", "0")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("Failed to execute gitgrip subprocess")?;

    let stdout = child
        .stdout
        .take()
        .context("Failed to capture subprocess stdout")?;
    let stderr = child
        .stderr
        .take()
        .context("Failed to capture subprocess stderr")?;

    let max_capture = max_capture_bytes();
    let err_thread = thread::spawn(move || capture_stream(stderr, max_capture));
    let parse_thread = thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        serde_json::from_reader::<_, Value>(&mut reader)
    });

    let cancel_status = start_cancel_controller(child.id(), cancel_flag);

    let status = child.wait().context("Failed waiting for subprocess")?;
    cancel_status.done.store(true, Ordering::SeqCst);
    let _ = cancel_status.join.join();
    let cancelled = cancel_status.kill_sent.load(Ordering::SeqCst);

    let context_json = parse_thread
        .join()
        .map_err(|_| anyhow::anyhow!("stdout parser thread panicked"))?;

    let (stderr, stderr_truncated) = err_thread
        .join()
        .map_err(|_| anyhow::anyhow!("stderr capture thread panicked"))??;
    let mut stderr = String::from_utf8_lossy(&stderr).to_string();
    if stderr_truncated {
        stderr.push_str(CAPTURE_TRUNCATED_MSG);
    }

    let context_json = match context_json {
        Ok(value) => Some(value),
        Err(err) => {
            if status.success() && !cancelled {
                return Err(anyhow::anyhow!(
                    "agent context produced non-JSON output: {err}"
                ));
            }
            None
        }
    };

    Ok(ContextCommandOutput {
        success: status.success() && !cancelled && context_json.is_some(),
        cancelled,
        status_code: status.code(),
        context_json,
        stderr,
    })
}

struct CancelController {
    done: Arc<AtomicBool>,
    kill_sent: Arc<AtomicBool>,
    join: thread::JoinHandle<()>,
}

fn start_cancel_controller(pid: u32, cancel_flag: Option<Arc<AtomicBool>>) -> CancelController {
    let done = Arc::new(AtomicBool::new(false));
    let kill_sent = Arc::new(AtomicBool::new(false));

    let done_clone = Arc::clone(&done);
    let kill_clone = Arc::clone(&kill_sent);
    let join = thread::spawn(move || {
        let Some(flag) = cancel_flag else {
            return;
        };

        while !done_clone.load(Ordering::SeqCst) {
            if flag.load(Ordering::SeqCst) {
                if kill_process(pid).is_ok() {
                    kill_clone.store(true, Ordering::SeqCst);
                }
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
    });

    CancelController {
        done,
        kill_sent,
        join,
    }
}

#[cfg(unix)]
fn kill_process(pid: u32) -> std::io::Result<()> {
    let pid_s = pid.to_string();
    let status = Command::new("kill").args(["-TERM", &pid_s]).status()?;
    if status.success() {
        return Ok(());
    }

    let status = Command::new("kill").args(["-KILL", &pid_s]).status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("failed to kill process"))
    }
}

#[cfg(windows)]
fn kill_process(pid: u32) -> std::io::Result<()> {
    let status = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/F"])
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("failed to kill process"))
    }
}

fn max_capture_bytes() -> usize {
    std::env::var(MAX_CAPTURE_ENV)
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_MAX_CAPTURE_BYTES)
}

fn capture_stream<R: Read>(mut reader: R, max_bytes: usize) -> anyhow::Result<(Vec<u8>, bool)> {
    let mut out = Vec::new();
    let mut buf = [0u8; 8192];
    let mut truncated = false;

    loop {
        let read = reader.read(&mut buf)?;
        if read == 0 {
            break;
        }

        if out.len() < max_bytes {
            let remaining = max_bytes - out.len();
            let keep = remaining.min(read);
            out.extend_from_slice(&buf[..keep]);
            if keep < read {
                truncated = true;
            }
        } else {
            truncated = true;
        }
    }

    Ok((out, truncated))
}

fn command_failure(label: &str, out: &CommandOutput) -> String {
    let mut message = if out.cancelled {
        format!("gitgrip {label} cancelled")
    } else {
        format!(
            "gitgrip {label} failed (exit code: {})",
            out.status_code
                .map(|c| c.to_string())
                .unwrap_or_else(|| "unknown".to_string())
        )
    };

    if let Some(details) = non_empty_output(&out.stdout, &out.stderr) {
        message.push('\n');
        message.push_str(&details);
    }

    message
}

fn command_failure_context(label: &str, out: &ContextCommandOutput) -> String {
    let mut message = if out.cancelled {
        format!("gitgrip {label} cancelled")
    } else {
        format!(
            "gitgrip {label} failed (exit code: {})",
            out.status_code
                .map(|c| c.to_string())
                .unwrap_or_else(|| "unknown".to_string())
        )
    };

    if !out.stderr.trim().is_empty() {
        message.push('\n');
        message.push_str(out.stderr.trim());
    }

    message
}

fn non_empty_output(stdout: &str, stderr: &str) -> Option<String> {
    let s_out = stdout.trim();
    let s_err = stderr.trim();
    match (s_out.is_empty(), s_err.is_empty()) {
        (true, true) => None,
        (false, true) => Some(s_out.to_string()),
        (true, false) => Some(s_err.to_string()),
        (false, false) => Some(format!("{s_out}\n{s_err}")),
    }
}

fn tools_definition() -> Vec<Value> {
    let mut tools = vec![
        json!({
            "name": "gitgrip_agent_context",
            "description": "Get gitgrip workspace/repo agent context as structured JSON.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repo name to filter context output."
                    }
                },
                "additionalProperties": false
            }
        }),
        json!({
            "name": "gitgrip_agent_build",
            "description": "Run agent.build command(s) defined in the gitgrip manifest.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repo name to build."
                    }
                },
                "additionalProperties": false
            }
        }),
        json!({
            "name": "gitgrip_agent_test",
            "description": "Run agent.test command(s) defined in the gitgrip manifest.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repo name to test."
                    }
                },
                "additionalProperties": false
            }
        }),
        json!({
            "name": "gitgrip_agent_verify",
            "description": "Run agent verification checks (build/test/lint) for configured repos.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repo name to verify."
                    }
                },
                "additionalProperties": false
            }
        }),
        json!({
            "name": "gitgrip_agent_generate_context",
            "description": "Generate context files for configured AI tool targets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, preview generated files without writing."
                    }
                },
                "additionalProperties": false
            }
        }),
    ];

    let passthrough_schema = json!({
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "Additional CLI args to pass after the command name."
            }
        },
        "additionalProperties": false
    });

    for spec in CLI_TOOL_SPECS {
        tools.push(json!({
            "name": spec.tool_name,
            "description": spec.description,
            "inputSchema": passthrough_schema
        }));
    }

    tools
}

fn find_cli_tool_spec(name: &str) -> Option<&'static CliToolSpec> {
    CLI_TOOL_SPECS.iter().find(|spec| spec.tool_name == name)
}

fn tool_text_response(
    text: &str,
    is_error: bool,
    structured_content: Option<Value>,
    cancelled: bool,
    status_code: Option<i32>,
) -> Value {
    let mut result = json!({
        "content": [
            {
                "type": "text",
                "text": text
            }
        ],
        "isError": is_error
    });

    let result_map = result
        .as_object_mut()
        .expect("tool response object must be an object");

    if let Some(content) = structured_content {
        result_map.insert("structuredContent".to_string(), content);
    }
    if cancelled {
        result_map.insert("cancelled".to_string(), Value::Bool(true));
    }
    if let Some(code) = status_code {
        result_map.insert("exitCode".to_string(), Value::Number(code.into()));
    }

    result
}

fn jsonrpc_result(id: Value, result: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    })
}

fn jsonrpc_error(id: Value, code: i64, message: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": code,
            "message": message
        }
    })
}

fn read_frame<R: BufRead>(reader: &mut R) -> anyhow::Result<Option<Vec<u8>>> {
    let mut content_length: Option<usize> = None;

    loop {
        let mut line = String::new();
        let bytes_read = reader.read_line(&mut line)?;
        if bytes_read == 0 {
            if content_length.is_none() {
                return Ok(None);
            }
            anyhow::bail!("Unexpected EOF while reading MCP headers");
        }

        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break;
        }

        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = Some(
                    value
                        .trim()
                        .parse::<usize>()
                        .context("Invalid Content-Length header value")?,
                );
            }
        }
    }

    let len = content_length.context("Missing Content-Length header")?;
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload)?;
    Ok(Some(payload))
}

fn resources_definition() -> Vec<Value> {
    vec![
        json!({
            "uri": "gitgrip://status",
            "name": "Workspace Status",
            "description": "Current status of all repositories (branches, changes, sync state).",
            "mimeType": "text/plain"
        }),
        json!({
            "uri": "gitgrip://manifest",
            "name": "Workspace Manifest",
            "description": "The gripspace.yml manifest defining repos, scripts, hooks, and settings.",
            "mimeType": "text/yaml"
        }),
        json!({
            "uri": "gitgrip://repos",
            "name": "Repository List",
            "description": "List of all repositories with paths and remotes.",
            "mimeType": "text/plain"
        }),
        json!({
            "uri": "gitgrip://scripts",
            "name": "Workspace Scripts",
            "description": "Available workspace scripts that can be run with `gr run`.",
            "mimeType": "text/plain"
        }),
        json!({
            "uri": "gitgrip://agent-context",
            "name": "Agent Context",
            "description": "Structured workspace context for AI agents (JSON).",
            "mimeType": "application/json"
        }),
    ]
}

fn handle_resource_read(uri: &str) -> Value {
    match uri {
        "gitgrip://status" => read_resource_from_command(uri, &["status"], "text/plain"),
        "gitgrip://manifest" => read_resource_from_manifest(),
        "gitgrip://repos" => read_resource_from_command(uri, &["repo", "list"], "text/plain"),
        "gitgrip://scripts" => read_resource_from_command(uri, &["run", "--list"], "text/plain"),
        "gitgrip://agent-context" => {
            read_resource_from_command(uri, &["--json", "agent", "context"], "application/json")
        }
        _ => jsonrpc_error_value(-32002, &format!("Resource not found: {uri}")),
    }
}

fn read_resource_from_command(uri: &str, args: &[&str], mime_type: &str) -> Value {
    let args: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    match run_gitgrip_command(&args, None) {
        Ok(out) => {
            let text = if out.success {
                non_empty_output(&out.stdout, &out.stderr)
                    .unwrap_or_else(|| "(no output)".to_string())
            } else {
                format!(
                    "Error (exit {}): {}",
                    out.status_code.unwrap_or(-1),
                    non_empty_output(&out.stdout, &out.stderr).unwrap_or_default()
                )
            };
            json!({
                "contents": [{
                    "uri": uri,
                    "mimeType": mime_type,
                    "text": text
                }]
            })
        }
        Err(err) => json!({
            "contents": [{
                "uri": uri,
                "mimeType": "text/plain",
                "text": format!("Failed to read resource: {err}")
            }]
        }),
    }
}

fn read_resource_from_manifest() -> Value {
    use crate::core::manifest_paths;

    let cwd = std::env::current_dir().unwrap_or_default();
    let Some(manifest_path) = manifest_paths::resolve_gripspace_manifest_path(&cwd) else {
        return json!({
            "contents": [{
                "uri": "gitgrip://manifest",
                "mimeType": "text/plain",
                "text": "No manifest found in current workspace."
            }]
        });
    };

    match std::fs::read_to_string(&manifest_path) {
        Ok(content) => json!({
            "contents": [{
                "uri": "gitgrip://manifest",
                "mimeType": "text/yaml",
                "text": content
            }]
        }),
        Err(err) => json!({
            "contents": [{
                "uri": "gitgrip://manifest",
                "mimeType": "text/plain",
                "text": format!("Failed to read manifest: {err}")
            }]
        }),
    }
}

fn jsonrpc_error_value(code: i64, message: &str) -> Value {
    json!({
        "error": {
            "code": code,
            "message": message
        }
    })
}

fn write_frame<W: Write>(writer: &mut W, message: &Value) -> anyhow::Result<()> {
    let body = serde_json::to_vec(message)?;
    write!(writer, "Content-Length: {}\r\n\r\n", body.len())?;
    writer.write_all(&body)?;
    writer.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_tool_list_has_expected_tools() {
        let tools = tools_definition();
        let names: Vec<String> = tools
            .iter()
            .filter_map(|t| t.get("name").and_then(|n| n.as_str()).map(str::to_string))
            .collect();

        assert!(names.contains(&"gitgrip_agent_context".to_string()));
        assert!(names.contains(&"gitgrip_agent_build".to_string()));
        assert!(names.contains(&"gitgrip_agent_test".to_string()));
        assert!(names.contains(&"gitgrip_agent_verify".to_string()));
        assert!(names.contains(&"gitgrip_agent_generate_context".to_string()));
        assert!(names.contains(&"gitgrip_sync".to_string()));
        assert!(names.contains(&"gitgrip_pr".to_string()));
        assert!(names.contains(&"gitgrip_issue".to_string()));
        assert!(names.contains(&"gitgrip_manifest".to_string()));
    }

    #[test]
    fn test_read_frame_parses_valid_message() {
        let payload = br#"{"jsonrpc":"2.0","id":1,"method":"ping"}"#;
        let input = format!(
            "Content-Length: {}\r\nContent-Type: application/json\r\n\r\n{}",
            payload.len(),
            String::from_utf8_lossy(payload)
        );

        let mut reader = BufReader::new(input.as_bytes());
        let read = read_frame(&mut reader).unwrap().unwrap();
        assert_eq!(read, payload);
    }

    #[test]
    fn test_tool_call_rejects_unknown_arguments() {
        let response = handle_tool_call(
            json!(1),
            json!({
                "name": "gitgrip_agent_build",
                "arguments": {
                    "repo": "app",
                    "unexpected": true
                }
            }),
            None,
        );

        let is_error = response
            .get("result")
            .and_then(|r| r.get("isError"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        assert!(is_error, "expected tool call to fail on unknown arguments");
    }

    #[test]
    fn test_resources_list_returns_defined_resources() {
        let response = handle_request(RpcRequest {
            jsonrpc: Some("2.0".to_string()),
            id: Some(json!(1)),
            method: "resources/list".to_string(),
            params: json!({}),
        })
        .expect("resources/list should return a response");

        let resources = response
            .get("result")
            .and_then(|r| r.get("resources"))
            .and_then(|v| v.as_array())
            .expect("resources should be an array");

        assert_eq!(resources.len(), 5);

        let uris: Vec<&str> = resources
            .iter()
            .filter_map(|r| r.get("uri").and_then(|v| v.as_str()))
            .collect();
        assert!(uris.contains(&"gitgrip://status"));
        assert!(uris.contains(&"gitgrip://manifest"));
        assert!(uris.contains(&"gitgrip://repos"));
        assert!(uris.contains(&"gitgrip://scripts"));
        assert!(uris.contains(&"gitgrip://agent-context"));
    }

    #[test]
    fn test_resources_templates_list_returns_empty() {
        let response = handle_request(RpcRequest {
            jsonrpc: Some("2.0".to_string()),
            id: Some(json!(1)),
            method: "resources/templates/list".to_string(),
            params: json!({}),
        })
        .expect("resources/templates/list should return a response");

        assert_eq!(
            response
                .get("result")
                .and_then(|r| r.get("resourceTemplates"))
                .and_then(|v| v.as_array())
                .map(|a| a.len()),
            Some(0)
        );
    }

    #[test]
    fn test_resource_read_unknown_uri_returns_error() {
        let response = handle_request(RpcRequest {
            jsonrpc: Some("2.0".to_string()),
            id: Some(json!(1)),
            method: "resources/read".to_string(),
            params: json!({ "uri": "gitgrip://nope" }),
        })
        .expect("resources/read should return a response");

        assert_eq!(
            response
                .get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_i64()),
            Some(-32002)
        );
    }

    #[test]
    fn test_resource_read_missing_uri_returns_error() {
        let response = handle_request(RpcRequest {
            jsonrpc: Some("2.0".to_string()),
            id: Some(json!(1)),
            method: "resources/read".to_string(),
            params: json!({}),
        })
        .expect("resources/read should return a response");

        assert_eq!(
            response
                .get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_i64()),
            Some(-32602)
        );
    }

    #[test]
    fn test_capture_stream_truncates() {
        let max = 1024;
        let input = vec![b'a'; max + 128];
        let (captured, truncated) = capture_stream(Cursor::new(input), max).unwrap();
        assert_eq!(captured.len(), max);
        assert!(truncated);
    }

    #[test]
    fn test_cancel_notification_sets_flag() {
        let request_id = json!(123);
        let key = request_id_key(&request_id);
        let flag = Arc::new(AtomicBool::new(false));
        let mut map = HashMap::new();
        map.insert(key, Arc::clone(&flag));

        handle_cancel_notification(json!({ "requestId": 123 }), &map);

        assert!(flag.load(Ordering::SeqCst));
    }
}
