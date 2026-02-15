//! Output abstraction for testability
//!
//! Provides [`OutputSink`] trait so command handlers can emit output through
//! an injectable interface instead of calling [`Output`] static methods
//! directly. Production code uses [`TerminalSink`]; tests use [`BufferSink`]
//! to capture output without touching stdout/stderr.

use std::sync::{Arc, Mutex};

use super::output::Output;

/// Trait for structured output from command handlers.
///
/// Each method corresponds to an [`Output`] static method. Implementations
/// control *where* the output goes — terminal, buffer, JSON, etc.
pub trait OutputSink: Send + Sync {
    fn success(&self, msg: &str);
    fn error(&self, msg: &str);
    fn warning(&self, msg: &str);
    fn info(&self, msg: &str);
    fn header(&self, msg: &str);
    fn subheader(&self, msg: &str);
    fn kv(&self, key: &str, value: &str);
    fn list_item(&self, item: &str);
    fn numbered_item(&self, num: usize, item: &str);
    fn diff_add(&self, line: &str);
    fn diff_del(&self, line: &str);

    /// Whether non-essential output should be suppressed.
    fn is_quiet(&self) -> bool;

    /// Whether output should be machine-readable JSON.
    fn is_json(&self) -> bool;
}

// ---------------------------------------------------------------------------
// TerminalSink — delegates to Output static methods (production)
// ---------------------------------------------------------------------------

/// Writes to stdout/stderr via the existing [`Output`] helpers.
pub struct TerminalSink {
    quiet: bool,
    json: bool,
}

impl TerminalSink {
    pub fn new(quiet: bool, json: bool) -> Self {
        Self { quiet, json }
    }
}

impl OutputSink for TerminalSink {
    fn success(&self, msg: &str) {
        Output::success(msg);
    }
    fn error(&self, msg: &str) {
        Output::error(msg);
    }
    fn warning(&self, msg: &str) {
        Output::warning(msg);
    }
    fn info(&self, msg: &str) {
        Output::info(msg);
    }
    fn header(&self, msg: &str) {
        Output::header(msg);
    }
    fn subheader(&self, msg: &str) {
        Output::subheader(msg);
    }
    fn kv(&self, key: &str, value: &str) {
        Output::kv(key, value);
    }
    fn list_item(&self, item: &str) {
        Output::list_item(item);
    }
    fn numbered_item(&self, num: usize, item: &str) {
        Output::numbered_item(num, item);
    }
    fn diff_add(&self, line: &str) {
        Output::diff_add(line);
    }
    fn diff_del(&self, line: &str) {
        Output::diff_del(line);
    }
    fn is_quiet(&self) -> bool {
        self.quiet
    }
    fn is_json(&self) -> bool {
        self.json
    }
}

// ---------------------------------------------------------------------------
// BufferSink — captures output for testing
// ---------------------------------------------------------------------------

/// Captures all output into an in-memory buffer for assertions in tests.
pub struct BufferSink {
    buffer: Arc<Mutex<Vec<String>>>,
    quiet: bool,
    json: bool,
}

impl BufferSink {
    pub fn new() -> Self {
        Self {
            buffer: Arc::new(Mutex::new(Vec::new())),
            quiet: false,
            json: false,
        }
    }

    pub fn with_quiet(mut self, quiet: bool) -> Self {
        self.quiet = quiet;
        self
    }

    pub fn with_json(mut self, json: bool) -> Self {
        self.json = json;
        self
    }

    /// Return a snapshot of all captured lines.
    pub fn lines(&self) -> Vec<String> {
        self.buffer.lock().unwrap().clone()
    }

    /// Return captured output joined with newlines.
    pub fn output(&self) -> String {
        self.buffer.lock().unwrap().join("\n")
    }

    fn push(&self, tag: &str, msg: &str) {
        self.buffer
            .lock()
            .unwrap()
            .push(format!("[{}] {}", tag, msg));
    }
}

impl Default for BufferSink {
    fn default() -> Self {
        Self::new()
    }
}

impl OutputSink for BufferSink {
    fn success(&self, msg: &str) {
        self.push("success", msg);
    }
    fn error(&self, msg: &str) {
        self.push("error", msg);
    }
    fn warning(&self, msg: &str) {
        self.push("warning", msg);
    }
    fn info(&self, msg: &str) {
        self.push("info", msg);
    }
    fn header(&self, msg: &str) {
        self.push("header", msg);
    }
    fn subheader(&self, msg: &str) {
        self.push("subheader", msg);
    }
    fn kv(&self, key: &str, value: &str) {
        self.push("kv", &format!("{}: {}", key, value));
    }
    fn list_item(&self, item: &str) {
        self.push("list", item);
    }
    fn numbered_item(&self, num: usize, item: &str) {
        self.push("list", &format!("{}. {}", num, item));
    }
    fn diff_add(&self, line: &str) {
        self.push("diff+", line);
    }
    fn diff_del(&self, line: &str) {
        self.push("diff-", line);
    }
    fn is_quiet(&self) -> bool {
        self.quiet
    }
    fn is_json(&self) -> bool {
        self.json
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buffer_sink_captures_output() {
        let sink = BufferSink::new();
        sink.success("done");
        sink.error("fail");
        sink.warning("careful");
        sink.info("note");

        let lines = sink.lines();
        assert_eq!(lines.len(), 4);
        assert_eq!(lines[0], "[success] done");
        assert_eq!(lines[1], "[error] fail");
        assert_eq!(lines[2], "[warning] careful");
        assert_eq!(lines[3], "[info] note");
    }

    #[test]
    fn buffer_sink_captures_kv() {
        let sink = BufferSink::new();
        sink.kv("branch", "main");
        sink.kv("status", "clean");

        assert_eq!(sink.lines(), vec!["[kv] branch: main", "[kv] status: clean"]);
    }

    #[test]
    fn buffer_sink_captures_list_items() {
        let sink = BufferSink::new();
        sink.list_item("alpha");
        sink.numbered_item(1, "beta");

        assert_eq!(sink.lines(), vec!["[list] alpha", "[list] 1. beta"]);
    }

    #[test]
    fn buffer_sink_captures_diff() {
        let sink = BufferSink::new();
        sink.diff_add("new line");
        sink.diff_del("old line");

        assert_eq!(sink.lines(), vec!["[diff+] new line", "[diff-] old line"]);
    }

    #[test]
    fn buffer_sink_output_joins_lines() {
        let sink = BufferSink::new();
        sink.success("a");
        sink.success("b");

        assert_eq!(sink.output(), "[success] a\n[success] b");
    }

    #[test]
    fn buffer_sink_quiet_and_json_flags() {
        let sink = BufferSink::new().with_quiet(true).with_json(true);
        assert!(sink.is_quiet());
        assert!(sink.is_json());

        let default_sink = BufferSink::new();
        assert!(!default_sink.is_quiet());
        assert!(!default_sink.is_json());
    }

    #[test]
    fn terminal_sink_flags() {
        let sink = TerminalSink::new(true, false);
        assert!(sink.is_quiet());
        assert!(!sink.is_json());
    }

    #[test]
    fn buffer_sink_header_and_subheader() {
        let sink = BufferSink::new();
        sink.header("Section");
        sink.subheader("details");

        assert_eq!(sink.lines(), vec!["[header] Section", "[subheader] details"]);
    }
}
