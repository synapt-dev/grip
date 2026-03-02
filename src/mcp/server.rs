//! stdio MCP server for gitgrip agent operations.

use anyhow::Context;
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::process::{Command, Stdio};

const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

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
    status_code: Option<i32>,
    stdout: String,
    stderr: String,
}

/// Run the gitgrip MCP server over stdio.
pub fn run_mcp_server() -> anyhow::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = BufReader::new(stdin.lock());
    let mut writer = BufWriter::new(stdout.lock());

    while let Some(bytes) = read_frame(&mut reader)? {
        let request: RpcRequest = match serde_json::from_slice(&bytes) {
            Ok(req) => req,
            Err(err) => {
                let response = jsonrpc_error(Value::Null, -32700, &format!("Parse error: {err}"));
                write_frame(&mut writer, &response)?;
                continue;
            }
        };

        let maybe_response = handle_request(request);
        if let Some(response) = maybe_response {
            write_frame(&mut writer, &response)?;
        }
    }

    Ok(())
}

fn handle_request(request: RpcRequest) -> Option<Value> {
    let id = request.id;

    if id.is_none() {
        return None;
    }

    let response = match request.method.as_str() {
        "initialize" => {
            let result = json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {
                        "listChanged": false
                    }
                },
                "serverInfo": {
                    "name": "gitgrip",
                    "version": env!("CARGO_PKG_VERSION")
                }
            });
            Some(jsonrpc_result(id.clone().unwrap_or(Value::Null), result))
        }
        "notifications/initialized" => None,
        "notifications/cancelled" => None,
        "ping" => Some(jsonrpc_result(id.clone().unwrap_or(Value::Null), json!({}))),
        "tools/list" => {
            let result = json!({
                "tools": tools_definition()
            });
            Some(jsonrpc_result(id.clone().unwrap_or(Value::Null), result))
        }
        "tools/call" => Some(handle_tool_call(
            id.clone().unwrap_or(Value::Null),
            request.params,
        )),
        _ => Some(jsonrpc_error(
            id.clone().unwrap_or(Value::Null),
            -32601,
            &format!("Method not found: {}", request.method),
        )),
    };

    response
}

fn handle_tool_call(id: Value, params: Value) -> Value {
    let parsed: ToolCallParams = match serde_json::from_value(params) {
        Ok(v) => v,
        Err(err) => {
            return jsonrpc_error(id, -32602, &format!("Invalid params for tools/call: {err}"));
        }
    };

    let tool_result: anyhow::Result<Value> = (|| match parsed.name.as_str() {
        "gitgrip_agent_context" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_context_tool(args.repo)
        }
        "gitgrip_agent_build" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("build", args.repo, &["agent", "build"])
        }
        "gitgrip_agent_test" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("test", args.repo, &["agent", "test"])
        }
        "gitgrip_agent_verify" => {
            let args: RepoArgs = parse_tool_args(parsed.arguments)?;
            run_text_tool("verify", args.repo, &["agent", "verify"])
        }
        "gitgrip_agent_generate_context" => {
            let args: GenerateContextArgs = parse_tool_args(parsed.arguments)?;
            let mut cmd = vec!["agent".to_string(), "generate-context".to_string()];
            if args.dry_run {
                cmd.push("--dry-run".to_string());
            }
            run_command_tool("generate-context", &cmd, None)
        }
        _ => Err(anyhow::anyhow!("Unknown tool: {}", parsed.name)),
    })();

    match tool_result {
        Ok(result) => jsonrpc_result(id, result),
        Err(err) => jsonrpc_result(id, tool_text_response(&err.to_string(), true, None)),
    }
}

fn run_context_tool(repo: Option<String>) -> anyhow::Result<Value> {
    let mut args = vec![
        "--json".to_string(),
        "agent".to_string(),
        "context".to_string(),
    ];
    if let Some(repo) = repo {
        args.push("--repo".to_string());
        args.push(repo);
    }

    let out = run_gitgrip_command(&args)?;
    if !out.success {
        let text = command_failure("context", &out);
        return Ok(tool_text_response(&text, true, None));
    }

    let stdout = out.stdout.trim();
    let parsed_json: Value = serde_json::from_str(stdout).with_context(|| {
        format!(
            "agent context produced non-JSON output. stdout:\n{}",
            if stdout.is_empty() { "<empty>" } else { stdout }
        )
    })?;

    let pretty = serde_json::to_string_pretty(&parsed_json)?;
    Ok(tool_text_response(&pretty, false, Some(parsed_json)))
}

fn run_text_tool(label: &str, repo: Option<String>, base_args: &[&str]) -> anyhow::Result<Value> {
    let mut args: Vec<String> = base_args.iter().map(|s| s.to_string()).collect();
    if let Some(repo) = repo {
        args.push(repo);
    }
    run_command_tool(label, &args, None)
}

fn run_command_tool(
    label: &str,
    args: &[String],
    structured: Option<Value>,
) -> anyhow::Result<Value> {
    let out = run_gitgrip_command(args)?;
    if out.success {
        let text = non_empty_output(&out.stdout, &out.stderr)
            .unwrap_or_else(|| format!("gitgrip agent {label} completed successfully"));
        Ok(tool_text_response(&text, false, structured))
    } else {
        let text = command_failure(label, &out);
        Ok(tool_text_response(&text, true, structured))
    }
}

fn parse_tool_args<T: for<'de> Deserialize<'de>>(value: Value) -> anyhow::Result<T> {
    if value.is_null() {
        return serde_json::from_value(json!({})).context("Failed to parse default args");
    }
    serde_json::from_value(value).context("Invalid tool arguments")
}

fn run_gitgrip_command(args: &[String]) -> anyhow::Result<CommandOutput> {
    let exe = std::env::current_exe().context("Failed to locate current gitgrip executable")?;
    let output = Command::new(exe)
        .args(args)
        .env("NO_COLOR", "1")
        .env("CLICOLOR", "0")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .context("Failed to execute gitgrip subprocess")?;

    Ok(CommandOutput {
        success: output.status.success(),
        status_code: output.status.code(),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

fn command_failure(label: &str, out: &CommandOutput) -> String {
    let mut message = format!(
        "gitgrip agent {label} failed (exit code: {})",
        out.status_code
            .map(|c| c.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    );

    if let Some(details) = non_empty_output(&out.stdout, &out.stderr) {
        message.push('\n');
        message.push_str(&details);
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
    vec![
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
    ]
}

fn tool_text_response(text: &str, is_error: bool, structured_content: Option<Value>) -> Value {
    let mut result = json!({
        "content": [
            {
                "type": "text",
                "text": text
            }
        ],
        "isError": is_error
    });

    if let Some(content) = structured_content {
        let result_map = result
            .as_object_mut()
            .expect("tool response object must be an object");
        result_map.insert("structuredContent".to_string(), content);
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

        let line = line.trim_end_matches(|c| c == '\r' || c == '\n');
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
        );

        let is_error = response
            .get("result")
            .and_then(|r| r.get("isError"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        assert!(is_error, "expected tool call to fail on unknown arguments");
    }
}
