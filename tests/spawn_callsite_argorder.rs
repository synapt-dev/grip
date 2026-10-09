//! Witness that `gr spawn up` composes tool-level args
//! BEFORE agent-level args at the CALL SITE (`run_spawn_up`'s
//! `assemble_launch_parts(...)` invocation), not merely inside the function.
//!
//! The unit test `launch_parts_compose_tool_args_before_agent_args` pins the
//! function's internal order; it would stay green if someone swapped the two
//! argument expressions at the call site (`&resolved_defaults` /
//! `&resolved_args`). This drives the REAL path — `gr spawn up --verbose` on a
//! two-entry agents.toml carrying a tool-level `--tool-flag` and an
//! agent-level `--agent-flag` — and asserts `--tool-flag` precedes
//! `--agent-flag` in the printed composed launch command (and in the argv the
//! launched process actually received). A call-site swap reverses both.
//!
//! `#[ignore]` by default: it needs a real tmux, which CI does not provide.
//! Run on a dev host:
//!   cargo test --test spawn_callsite_argorder -- --ignored
//!
//! FLEET-KILL SAFETY (config claude.md, "The default tmux socket is the live
//! fleet"): the child `gr` process has TMUX removed from its environment and
//! TMUX_TMPDIR pointed at a fresh temp dir, so every tmux call gr makes lands
//! on a PRIVATE server, never the default fleet socket; the session name is
//! unique and never "synapt"; teardown kills only that private server, with
//! the same isolation on the kill command. `env_remove("TMUX")` is the
//! load-bearing half — inside a pane $TMUX overrides TMUX_TMPDIR.
//!
//! Unix-only: the stub tool is a `#!/usr/bin/env bash` script made executable
//! via `PermissionsExt::from_mode`, and the test drives a real tmux — none of
//! which exists on Windows. `#![cfg(unix)]` excludes the whole file from the
//! Windows build; `#[ignore]` alone would still COMPILE it (skipping only
//! execution), and `std::os::unix` does not exist on Windows, so an ignored
//! test still red the `windows-latest` `cargo test` build (grip#931-adjacent,
//! measured on dev CI run 34364897339: E0433 `os::unix`, E0599 `from_mode`).
#![cfg(unix)]

use assert_cmd::prelude::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tempfile::TempDir;

#[test]
#[ignore = "requires real tmux; run with --ignored on a dev host"]
fn spawn_up_composes_tool_args_before_agent_args_at_call_site() {
    let ws = TempDir::new().unwrap();
    let tmux_tmp = TempDir::new().unwrap();
    let root = ws.path();
    fs::create_dir_all(root.join(".gitgrip")).unwrap();

    // Stub tool binary: records the argv it was launched with, then stays
    // alive so the pane survives gr's launch-verify probe.
    let stub = root.join("mocktool.sh");
    let argv_out = root.join("argv.out");
    fs::write(
        &stub,
        format!(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > '{}'\nexec sleep 30\n",
            argv_out.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();

    let session = format!("grtest-callsite-{}", std::process::id());
    let toml = root.join(".gitgrip/agents.toml");
    fs::write(
        &toml,
        format!(
            "[spawn]\nsession_name = \"{session}\"\n\
             [tools.mock]\nbinary = \"{stub}\"\nargs = [\"--tool-flag\"]\n\
             [agents.probe]\nrole = \"worker\"\ntool = \"mock\"\nargs = [\"--agent-flag\"]\n",
            stub = stub.display(),
        ),
    )
    .unwrap();

    let out = Command::cargo_bin("gr1")
        .unwrap()
        .args(["spawn", "up", "--verbose", "--config"])
        .arg(&toml)
        .current_dir(root)
        .env_remove("TMUX")
        .env("TMUX_TMPDIR", tmux_tmp.path())
        .output()
        .expect("run gr spawn up");

    // Teardown: kill ONLY the private server (same isolation as the child).
    let _ = Command::new("tmux")
        .arg("kill-server")
        .env_remove("TMUX")
        .env("TMUX_TMPDIR", tmux_tmp.path())
        .status();

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let line = combined
        .lines()
        .find(|l| l.contains("launch command:"))
        .unwrap_or_else(|| panic!("no 'launch command:' verbose line in gr output:\n{combined}"));
    let ti = line
        .find("--tool-flag")
        .unwrap_or_else(|| panic!("--tool-flag missing from composed command: {line}"));
    let ai = line
        .find("--agent-flag")
        .unwrap_or_else(|| panic!("--agent-flag missing from composed command: {line}"));
    assert!(
        ti < ai,
        "call-site composition: tool-level args must precede agent-level args; got: {line}"
    );

    // Secondary: the argv the process actually received matches that order.
    if let Ok(argv) = fs::read_to_string(&argv_out) {
        if let (Some(ti2), Some(ai2)) = (argv.find("--tool-flag"), argv.find("--agent-flag")) {
            assert!(
                ti2 < ai2,
                "launched argv order disagrees with tool-before-agent: {argv}"
            );
        }
    }
}
