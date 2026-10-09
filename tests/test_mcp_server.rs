use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tempfile::TempDir;

const RESPONSE_READ_DEADLINE: Duration = Duration::from_secs(10);

type ResponseLine = Result<String, String>;

#[derive(Debug, PartialEq)]
enum ResponseReadError {
    Deadline(Duration),
    IncompleteFrame,
    Reader(String),
    Disconnected,
}

struct ResponseReader {
    lines: Receiver<ResponseLine>,
    deadline: Duration,
}

impl ResponseReader {
    fn new(lines: Receiver<ResponseLine>, deadline: Duration) -> Self {
        Self { lines, deadline }
    }

    fn recv(&self) -> Result<String, ResponseReadError> {
        match self.lines.recv_timeout(self.deadline) {
            Ok(Ok(line)) if line.ends_with('\n') => Ok(line),
            Ok(Ok(_)) => Err(ResponseReadError::IncompleteFrame),
            Ok(Err(error)) => Err(ResponseReadError::Reader(error)),
            Err(RecvTimeoutError::Timeout) => Err(ResponseReadError::Deadline(self.deadline)),
            Err(RecvTimeoutError::Disconnected) => Err(ResponseReadError::Disconnected),
        }
    }
}

fn spawn_response_reader<R>(mut reader: R) -> (Receiver<ResponseLine>, JoinHandle<()>)
where
    R: BufRead + Send + 'static,
{
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => {
                let _ = sender.send(Ok(line));
                break;
            }
            Ok(_) => {
                if sender.send(Ok(line)).is_err() {
                    break;
                }
            }
            Err(error) => {
                let _ = sender.send(Err(error.to_string()));
                break;
            }
        }
    });
    (receiver, handle)
}

struct ServerHarness {
    child: Child,
    stdin: Option<ChildStdin>,
    responses: ResponseReader,
}

impl ServerHarness {
    fn spawn(cwd: &Path, envs: &[(&str, &str)]) -> Self {
        Self::spawn_with_deadline(cwd, envs, RESPONSE_READ_DEADLINE)
    }

    fn spawn_with_deadline(cwd: &Path, envs: &[(&str, &str)], response_deadline: Duration) -> Self {
        Self::spawn_exe(env!("CARGO_BIN_EXE_gitgrip"), cwd, envs, response_deadline)
    }

    fn spawn_exe(
        exe: &str,
        cwd: &Path,
        envs: &[(&str, &str)],
        response_deadline: Duration,
    ) -> Self {
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
        let (responses, _reader) = spawn_response_reader(BufReader::new(stdout));
        Self {
            child,
            stdin: Some(stdin),
            responses: ResponseReader::new(responses, response_deadline),
        }
    }

    fn send(&mut self, payload: &Value) {
        let bytes = serde_json::to_vec(payload).expect("serialize payload");
        let stdin = self.stdin.as_mut().expect("server stdin available");
        stdin.write_all(&bytes).expect("write payload");
        stdin.write_all(b"\n").expect("write delimiter");
        stdin.flush().expect("flush stdin");
    }

    fn send_raw_json_line(&mut self, raw_payload: &[u8]) {
        let stdin = self.stdin.as_mut().expect("server stdin available");
        stdin.write_all(raw_payload).expect("write raw payload");
        stdin.write_all(b"\n").expect("write delimiter");
        stdin.flush().expect("flush stdin");
    }

    fn send_unterminated(&mut self, raw_payload: &[u8]) {
        let stdin = self.stdin.as_mut().expect("server stdin available");
        stdin.write_all(raw_payload).expect("write raw payload");
        stdin.flush().expect("flush stdin");
    }

    fn recv(&mut self) -> Value {
        let line = self
            .responses
            .recv()
            .unwrap_or_else(|error| panic!("read newline-delimited response: {error:?}"));
        assert!(!line.is_empty(), "unexpected EOF while reading response");
        let trimmed = line.trim_end_matches(['\r', '\n']);
        serde_json::from_str(trimmed).unwrap_or_else(|error| {
            panic!(
                "parse JSON response line (len {}): {trimmed:?} -- {error}",
                trimmed.len()
            )
        })
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
        self.stdin.take();

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

impl Drop for ServerHarness {
    fn drop(&mut self) {
        self.stdin.take();
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

struct ChannelReader {
    bytes: Receiver<Vec<u8>>,
    pending: VecDeque<u8>,
}

impl Read for ChannelReader {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        while self.pending.is_empty() {
            match self.bytes.recv() {
                Ok(bytes) => self.pending.extend(bytes),
                Err(_) => return Ok(0),
            }
        }
        let count = buffer.len().min(self.pending.len());
        for (slot, byte) in buffer.iter_mut().zip(self.pending.drain(..count)) {
            *slot = byte;
        }
        Ok(count)
    }
}

#[test]
fn test_response_reader_times_out_on_incomplete_frame() {
    let inner_deadline = Duration::from_millis(100);
    let outer_deadline = Duration::from_secs(5);
    let (bytes_sender, bytes_receiver) = mpsc::channel();
    let source = ChannelReader {
        bytes: bytes_receiver,
        pending: VecDeque::new(),
    };
    let (lines, reader) = spawn_response_reader(BufReader::new(source));
    let responses = ResponseReader::new(lines, inner_deadline);

    bytes_sender
        .send(br#"{"jsonrpc":"2.0","id":1}"#.to_vec())
        .expect("send unterminated response bytes");

    let (outcome_sender, outcome_receiver) = mpsc::channel();
    let waiter = thread::spawn(move || {
        let started = Instant::now();
        let outcome = responses.recv();
        let _ = outcome_sender.send((outcome, started.elapsed()));
    });

    let (outcome, elapsed) = outcome_receiver
        .recv_timeout(outer_deadline)
        .expect("deadline mechanism itself must not hang the test");
    assert_eq!(outcome, Err(ResponseReadError::Deadline(inner_deadline)));
    assert!(elapsed >= inner_deadline);
    assert!(elapsed < outer_deadline);

    drop(bytes_sender);
    reader.join().expect("join response reader");
    waiter.join().expect("join deadline waiter");
}

#[test]
fn test_response_reader_rejects_partial_frame_at_eof() {
    let (bytes_sender, bytes_receiver) = mpsc::channel();
    let source = ChannelReader {
        bytes: bytes_receiver,
        pending: VecDeque::new(),
    };
    let (lines, reader) = spawn_response_reader(BufReader::new(source));
    let responses = ResponseReader::new(lines, Duration::from_secs(1));

    bytes_sender
        .send(br#"{"jsonrpc":"2.0","id":1}"#.to_vec())
        .expect("send unterminated response bytes");
    drop(bytes_sender);

    assert_eq!(responses.recv(), Err(ResponseReadError::IncompleteFrame));
    reader.join().expect("join response reader");
}

#[test]
fn test_server_harness_timeout_unwinds_without_waiting_for_reader() {
    let inner_deadline = Duration::from_millis(100);
    let outer_deadline = Duration::from_secs(5);
    let (outcome_sender, outcome_receiver) = mpsc::channel();

    let waiter = thread::spawn(move || {
        let outcome = std::panic::catch_unwind(|| {
            let temp = TempDir::new().expect("create temp dir");
            let mut server = ServerHarness::spawn_with_deadline(temp.path(), &[], inner_deadline);
            server.send_unterminated(br#"{"jsonrpc":"2.0","id":1}"#);
            let _ = server.recv();
        });
        let message = match outcome {
            Ok(()) => String::from("receive unexpectedly returned"),
            Err(payload) => payload
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| {
                    payload
                        .downcast_ref::<&str>()
                        .map(|value| (*value).to_owned())
                })
                .unwrap_or_else(|| String::from("non-string panic")),
        };
        let _ = outcome_sender.send(message);
    });

    let message = outcome_receiver
        .recv_timeout(outer_deadline)
        .expect("timeout and ServerHarness cleanup must finish without an external watchdog");
    assert!(
        message.contains("Deadline(100ms)"),
        "actual harness path must fail for its response deadline: {message}"
    );
    waiter.join().expect("join timeout-path witness");
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

    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "unsupported/method",
        "params": {}
    }));
    let unsupported = server.recv();
    assert_eq!(unsupported["id"], json!(4));
    assert_eq!(unsupported["error"]["code"], json!(-32601));

    server.shutdown();
}

/// The server started as `gr` (how a host config usually names it) re-runs itself for each tool as gr1. Outside any
/// workspace with no gr2 installed, the server falls back to gr1 and serves; a tool re-run that resolved again would
/// print the resolver's install line, and the tool result carries the re-run's stderr. GR2_QUIET_CONTEXT is forced off,
/// or that line would be silenced and the check could not fail.
#[cfg(unix)]
#[test]
fn test_mcp_server_started_as_gr_reruns_itself_as_gr1() {
    let temp = TempDir::new().expect("create temp dir");
    let mut server = ServerHarness::spawn_exe(
        env!("CARGO_BIN_EXE_gr"),
        temp.path(),
        &[("PATH", "/usr/bin:/bin"), ("GR2_QUIET_CONTEXT", "")],
        RESPONSE_READ_DEADLINE,
    );
    server.initialize();
    server.send(&json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": { "name": "gitgrip_agent_build", "arguments": {} }
    }));
    let call = server.recv();
    assert_eq!(call["id"], json!(2));
    let text = call["result"].to_string();
    assert!(
        !text.contains("gr: "),
        "the tool's re-run resolved again instead of running as gr1: {text}"
    );
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

    server.send_raw_json_line(br#"{"jsonrpc":"2.0","id":99,"method":"ping""#);
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
