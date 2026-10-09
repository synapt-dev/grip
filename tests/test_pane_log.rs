//! The worker executes as gr1, without resolving by the pane's workspace or executable filename.

#[cfg(unix)]
mod unix {
    use gitgrip::core::pane_log::{pipe_command, LIMIT_BYTES};
    use std::io::Write;
    use std::process::{Command, Stdio};

    #[test]
    fn real_worker_is_bounded_and_quiet_at_a_both_markers_root() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join(".gitgrip")).unwrap();
        std::fs::create_dir(dir.path().join(".git")).unwrap();
        std::fs::write(dir.path().join("grip.toml"), "").unwrap();
        let odd = dir.path().join("space's; touch SHOULD_NOT_EXIST");
        std::fs::create_dir(&odd).unwrap();
        let log = odd.join("output.log");
        let executable = std::path::Path::new(env!("CARGO_BIN_EXE_gr"));
        let command = pipe_command(executable, &log).unwrap();
        let mut child = Command::new("sh")
            .args(["-c", &command])
            .current_dir(dir.path())
            .env("PATH", "/usr/bin:/bin")
            .env("GR2_QUIET_CONTEXT", "")
            .env("GR_RESOLVED", "gr1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let chunk = b"\x1b[2K\r|".repeat(2048);
        let mut input = child.stdin.take().unwrap();
        for _ in 0..1024 {
            input.write_all(&chunk).unwrap();
        }
        drop(input);
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{:?}", output);
        assert!(output.stdout.is_empty());
        assert!(output.stderr.is_empty(), "{:?}", output);
        assert_eq!(std::fs::metadata(&log).unwrap().len(), LIMIT_BYTES);
        assert!(!std::fs::read(&log)
            .unwrap()
            .windows(4)
            .any(|w| w == b"gr: "));
        assert!(!dir.path().join("SHOULD_NOT_EXIST").exists());
    }

    #[test]
    fn malformed_worker_args_refuse_before_cli_or_runtime() {
        use std::os::unix::process::CommandExt;
        let output = Command::new(env!("CARGO_BIN_EXE_gr"))
            .arg0("gr1")
            .args(["--internal-pane-log"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert!(String::from_utf8_lossy(&output.stderr)
            .contains("pane capture needs exactly one log path"));
    }
}
