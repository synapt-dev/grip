use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::thread;
use std::time::Duration;
use tempfile::TempDir;

fn write_frame(stdin: &mut ChildStdin, payload: &Value) {
    let bytes = serde_json::to_vec(payload).expect("serialize payload");
    write!(stdin, "Content-Length: {}\r\n\r\n", bytes.len()).expect("write header");
    stdin.write_all(&bytes).expect("write payload");
    stdin.flush().expect("flush stdin");
}

fn read_frame(stdout: &mut BufReader<ChildStdout>) -> Value {
    let mut content_length: Option<usize> = None;

    loop {
        let mut line = String::new();
        let read = stdout.read_line(&mut line).expect("read frame header line");
        assert!(read > 0, "unexpected EOF while reading frame headers");

        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break;
        }

        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = Some(value.trim().parse::<usize>().expect("parse Content-Length"));
            }
        }
    }

    let len = content_length.expect("missing Content-Length");
    let mut body = vec![0u8; len];
    stdout.read_exact(&mut body).expect("read frame body");
    serde_json::from_slice(&body).expect("parse JSON payload")
}

fn shutdown(mut child: Child) {
    drop(child.stdin.take());

    for _ in 0..100 {
        if child.try_wait().expect("poll child").is_some() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }

    let _ = child.kill();
    let _ = child.wait();
}

fn write_minimal_workspace(root: &std::path::Path) {
    let spaces_main = root.join(".gitgrip").join("spaces").join("main");
    fs::create_dir_all(&spaces_main).expect("create spaces/main");
    fs::create_dir_all(root.join("app")).expect("create app dir");

    let manifest = r#"version: 1
repos:
  app:
    url: git@github.com:example/app.git
    path: ./app
    default_branch: main
    agent:
      build: sleep 5
"#;
    fs::write(spaces_main.join("gripspace.yml"), manifest).expect("write manifest");
}

#[test]
fn test_mcp_server_initialize_list_and_call() {
    let temp = TempDir::new().expect("create temp dir");
    let exe = env!("CARGO_BIN_EXE_gitgrip");

    let mut child = Command::new(exe)
        .args(["mcp", "server"])
        .current_dir(temp.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn mcp server");

    let mut stdin = child.stdin.take().expect("take stdin");
    let stdout = child.stdout.take().expect("take stdout");
    let mut reader = BufReader::new(stdout);

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        }),
    );
    let initialize = read_frame(&mut reader);
    assert_eq!(initialize["id"], json!(1));
    assert_eq!(initialize["result"]["protocolVersion"], json!("2024-11-05"));

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }),
    );
    let tools = read_frame(&mut reader);
    assert_eq!(tools["id"], json!(2));

    let names: Vec<&str> = tools["result"]["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .filter_map(|t| t.get("name").and_then(Value::as_str))
        .collect();
    assert!(names.contains(&"gitgrip_agent_context"));
    assert!(names.contains(&"gitgrip_agent_build"));

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "gitgrip_agent_build",
                "arguments": {}
            }
        }),
    );
    let call = read_frame(&mut reader);
    assert_eq!(call["id"], json!(3));
    assert_eq!(call["result"]["isError"], json!(true));

    drop(stdin);
    shutdown(child);
}

#[test]
fn test_mcp_server_cancelled_notification_interrupts_tool_call() {
    let temp = TempDir::new().expect("create temp dir");
    write_minimal_workspace(temp.path());
    let exe = env!("CARGO_BIN_EXE_gitgrip");

    let mut child = Command::new(exe)
        .args(["mcp", "server"])
        .current_dir(temp.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn mcp server");

    let mut stdin = child.stdin.take().expect("take stdin");
    let stdout = child.stdout.take().expect("take stdout");
    let mut reader = BufReader::new(stdout);

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        }),
    );
    let _ = read_frame(&mut reader);

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "gitgrip_agent_build",
                "arguments": {
                    "repo": "app"
                }
            }
        }),
    );

    thread::sleep(Duration::from_millis(150));

    write_frame(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": 42
            }
        }),
    );

    let response = read_frame(&mut reader);
    assert_eq!(response["id"], json!(42));
    assert_eq!(response["result"]["isError"], json!(true));
    let text = response["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_default();
    assert!(
        text.contains("cancelled"),
        "expected cancellation message, got: {text}"
    );

    drop(stdin);
    shutdown(child);
}
