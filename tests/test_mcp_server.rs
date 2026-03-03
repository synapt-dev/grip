use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::thread;
use std::time::Duration;
use tempfile::TempDir;

struct ServerHarness {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl ServerHarness {
    fn spawn(cwd: &Path, envs: &[(&str, &str)]) -> Self {
        let exe = env!("CARGO_BIN_EXE_gitgrip");
        let mut cmd = Command::new(exe);
        cmd.args(["mcp", "server"]).current_dir(cwd);
        for (k, v) in envs {
            cmd.env(k, v);
        }

        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn mcp server");

        let stdin = child.stdin.take().expect("take stdin");
        let stdout = child.stdout.take().expect("take stdout");
        Self {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        }
    }

    fn send(&mut self, payload: &Value) {
        let bytes = serde_json::to_vec(payload).expect("serialize payload");
        write!(self.stdin, "Content-Length: {}\r\n\r\n", bytes.len()).expect("write header");
        self.stdin.write_all(&bytes).expect("write payload");
        self.stdin.flush().expect("flush stdin");
    }

    fn send_raw_json_frame(&mut self, raw_payload: &[u8]) {
        write!(self.stdin, "Content-Length: {}\r\n\r\n", raw_payload.len()).expect("write header");
        self.stdin
            .write_all(raw_payload)
            .expect("write raw payload");
        self.stdin.flush().expect("flush stdin");
    }

    fn recv(&mut self) -> Value {
        let mut content_length: Option<usize> = None;

        loop {
            let mut line = String::new();
            let read = self
                .stdout
                .read_line(&mut line)
                .expect("read frame header line");
            assert!(read > 0, "unexpected EOF while reading frame headers");

            let line = line.trim_end_matches(['\r', '\n']);
            if line.is_empty() {
                break;
            }

            if let Some((name, value)) = line.split_once(':') {
                if name.trim().eq_ignore_ascii_case("content-length") {
                    content_length =
                        Some(value.trim().parse::<usize>().expect("parse Content-Length"));
                }
            }
        }

        let len = content_length.expect("missing Content-Length");
        let mut body = vec![0u8; len];
        self.stdout.read_exact(&mut body).expect("read frame body");
        serde_json::from_slice(&body).expect("parse JSON payload")
    }

    fn initialize(&mut self) {
        self.send(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        }));
        let initialize = self.recv();
        assert_eq!(initialize["id"], json!(1));
        assert_eq!(initialize["result"]["protocolVersion"], json!("2024-11-05"));
    }

    fn shutdown(mut self) {
        drop(self.stdin);

        for _ in 0..120 {
            if self.child.try_wait().expect("poll child").is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(20));
        }

        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn write_workspace_with_build_commands(root: &Path, repos: &[(&str, &str)]) {
    let spaces_main = root.join(".gitgrip").join("spaces").join("main");
    fs::create_dir_all(&spaces_main).expect("create spaces/main");

    let mut manifest = String::from("version: 1\nrepos:\n");
    for (name, cmd) in repos {
        fs::create_dir_all(root.join(name)).expect("create repo dir");
        manifest.push_str(&format!(
            "  {name}:\n    url: git@github.com:example/{name}.git\n    path: ./{name}\n    default_branch: main\n    agent:\n      build: \"{cmd}\"\n"
        ));
    }

    fs::write(spaces_main.join("gripspace.yml"), manifest).expect("write manifest");
}

fn write_large_context_workspace(root: &Path, repo_count: usize) {
    let spaces_main = root.join(".gitgrip").join("spaces").join("main");
    fs::create_dir_all(&spaces_main).expect("create spaces/main");

    let long_desc = "x".repeat(320);
    let mut manifest = String::from("version: 1\nrepos:\n");
    for i in 0..repo_count {
        let name = format!("repo_{i}");
        manifest.push_str(&format!(
            "  {name}:\n    url: git@github.com:example/{name}.git\n    path: ./{name}\n    default_branch: main\n    agent:\n      description: \"{long_desc}\"\n      build: \"echo ok\"\n"
        ));
    }

    fs::write(spaces_main.join("gripspace.yml"), manifest).expect("write manifest");
}

#[test]
fn test_mcp_server_initialize_list_and_call() {
    let temp = TempDir::new().expect("create temp dir");
    let mut server = ServerHarness::spawn(temp.path(), &[]);

    server.initialize();

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }));
    let tools = server.recv();
    assert_eq!(tools["id"], json!(2));

    let names: Vec<&str> = tools["result"]["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .filter_map(|t| t.get("name").and_then(Value::as_str))
        .collect();
    assert!(names.contains(&"gitgrip_agent_context"));
    assert!(names.contains(&"gitgrip_agent_build"));

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_build",
            "arguments": {}
        }
    }));
    let call = server.recv();
    assert_eq!(call["id"], json!(3));
    assert_eq!(call["result"]["isError"], json!(true));

    server.shutdown();
}

#[test]
fn test_mcp_server_cancel_immediate_and_ping_still_works() {
    let temp = TempDir::new().expect("create temp dir");
    write_workspace_with_build_commands(temp.path(), &[("app", "sleep 2")]);
    let mut server = ServerHarness::spawn(temp.path(), &[]);

    server.initialize();

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_build",
            "arguments": {
                "repo": "app"
            }
        }
    }));

    server.send(&json!({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {
            "requestId": 42
        }
    }));

    let response = server.recv();
    assert_eq!(response["id"], json!(42));
    assert_eq!(response["result"]["isError"], json!(true));
    assert_eq!(response["result"]["cancelled"], json!(true));

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 43,
        "method": "ping",
        "params": {}
    }));
    let ping = server.recv();
    assert_eq!(ping["id"], json!(43));
    assert_eq!(ping["result"], json!({}));

    server.shutdown();
}

#[test]
fn test_mcp_server_cancel_near_completion_is_safe() {
    let temp = TempDir::new().expect("create temp dir");
    write_workspace_with_build_commands(temp.path(), &[("app", "sleep 1")]);
    let mut server = ServerHarness::spawn(temp.path(), &[]);

    server.initialize();

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 50,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_build",
            "arguments": {
                "repo": "app"
            }
        }
    }));

    thread::sleep(Duration::from_millis(900));

    server.send(&json!({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {
            "requestId": 50
        }
    }));

    let response = server.recv();
    assert_eq!(response["id"], json!(50));
    let is_error = response["result"]["isError"].as_bool().unwrap_or(false);
    let cancelled = response["result"]
        .get("cancelled")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    assert!(
        (!is_error && !cancelled) || (is_error && cancelled),
        "near-completion cancel should be either clean success or explicit cancellation"
    );

    server.shutdown();
}

#[test]
fn test_mcp_server_malformed_json_frame_recovery() {
    let temp = TempDir::new().expect("create temp dir");
    let mut server = ServerHarness::spawn(temp.path(), &[]);

    server.initialize();

    server.send_raw_json_frame(br#"{"jsonrpc":"2.0","id":99,"method":"ping""#);
    let parse_err = server.recv();
    assert_eq!(parse_err["error"]["code"], json!(-32700));

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 100,
        "method": "ping",
        "params": {}
    }));
    let ping = server.recv();
    assert_eq!(ping["id"], json!(100));
    assert_eq!(ping["result"], json!({}));

    server.shutdown();
}

#[test]
fn test_mcp_server_concurrent_calls_with_one_cancelled() {
    let temp = TempDir::new().expect("create temp dir");
    write_workspace_with_build_commands(temp.path(), &[("slow", "sleep 3"), ("fast", "echo fast")]);
    let mut server = ServerHarness::spawn(temp.path(), &[]);

    server.initialize();

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 200,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_build",
            "arguments": {
                "repo": "slow"
            }
        }
    }));

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 201,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_build",
            "arguments": {
                "repo": "fast"
            }
        }
    }));

    thread::sleep(Duration::from_millis(120));
    server.send(&json!({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {
            "requestId": 200
        }
    }));

    let mut responses: HashMap<i64, Value> = HashMap::new();
    for _ in 0..2 {
        let resp = server.recv();
        let id = resp["id"].as_i64().expect("numeric id");
        responses.insert(id, resp);
    }

    let slow = responses.get(&200).expect("slow response present");
    let fast = responses.get(&201).expect("fast response present");

    assert_eq!(slow["result"]["isError"], json!(true));
    assert_eq!(slow["result"]["cancelled"], json!(true));
    assert_eq!(fast["result"]["isError"], json!(false));

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 202,
        "method": "ping",
        "params": {}
    }));
    let ping = server.recv();
    assert_eq!(ping["id"], json!(202));

    server.shutdown();
}

#[test]
fn test_mcp_server_large_context_ignores_small_capture_cap() {
    let temp = TempDir::new().expect("create temp dir");
    write_large_context_workspace(temp.path(), 500);

    // Force a very small cap to prove context does not rely on capped stdout capture.
    let mut server =
        ServerHarness::spawn(temp.path(), &[("GITGRIP_MCP_MAX_CAPTURE_BYTES", "2048")]);

    server.initialize();

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 300,
        "method": "tools/call",
        "params": {
            "name": "gitgrip_agent_context",
            "arguments": {}
        }
    }));

    let response = server.recv();
    assert_eq!(response["id"], json!(300));
    assert_eq!(response["result"]["isError"], json!(false));

    let repos = response["result"]["structuredContent"]["repos"]
        .as_array()
        .expect("repos array in structured content");
    assert_eq!(repos.len(), 500);

    server.shutdown();
}
