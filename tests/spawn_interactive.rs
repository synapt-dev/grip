//! `gr spawn up --interactive` (and the automatic fallback when tmux is absent)
//! runs ONE named agent in the foreground of the current terminal, and refuses
//! the whole fleet with no multiplexer while naming the single-agent form.
//!
//! Before this, `run_spawn_up` called `require_tmux()` first and bailed on any
//! box without tmux (native Windows, a bare container), so the verb never had a
//! path there. The foreground launch runs the SAME command a pane would (built
//! by the shared `build_agent_launch`), so these drive the real binary.
//!
//! The foreground launch runs the agent's command through `bash <launch-script>`,
//! exactly as a tmux pane does, so this file is Unix-only for its stub tool and
//! bash dependency (`#![cfg(unix)]`); on Windows the witnesses are the CI job and
//! Layne's box. No tmux is touched by the interactive path, so there is no
//! private-socket isolation to set up here (contrast spawn_callsite_argorder).

#![cfg(unix)]

use assert_cmd::prelude::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tempfile::TempDir;

/// A gripspace whose one tool is a stub that prints a marker and exits 0, so a
/// foreground launch completes instead of blocking.
///
/// `gr spawn up` registers each agent in `$HOME/.synapt/orgs/<org>/team.db`, an
/// org keyed on the config's `session_name`. Every `gr` invocation below pins
/// `HOME` to the gripspace's own tempdir, so each test writes to a fresh,
/// throwaway registry: distinct tempdirs never share an org, so the concurrent
/// `register_agent` calls of a parallel `cargo test` run cannot collide on the
/// `UNIQUE(org_id, display_name)` constraint (they did before this pin), and no
/// test touches the developer's real `~/.synapt`.
fn write_gripspace(agents: &str) -> TempDir {
    let ws = TempDir::new().unwrap();
    let root = ws.path();
    fs::create_dir_all(root.join(".gitgrip")).unwrap();

    let stub = root.join("echotool.sh");
    fs::write(&stub, "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n").unwrap();
    fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();

    let toml = format!(
        "[spawn]\nsession_name = \"grtest-interactive-{}\"\nchannel = \"dev\"\n\
         [tools.echotool]\nbinary = \"{stub}\"\n{agents}",
        std::process::id(),
        stub = stub.display(),
    );
    fs::write(root.join(".gitgrip/agents.toml"), toml).unwrap();
    ws
}

#[test]
fn interactive_single_agent_runs_in_foreground() {
    let ws = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\n\
         worktree = \".\"\nargs = [\"INTERACTIVE_FOREGROUND_OK\"]\n",
    );

    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up", "probe", "--interactive"])
        .current_dir(ws.path())
        .env("HOME", ws.path())
        .output()
        .expect("run gr spawn up probe --interactive");

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        out.status.success(),
        "interactive single-agent launch should exit 0; got {:?}\n{combined}",
        out.status.code()
    );
    assert!(
        combined.contains("INTERACTIVE_FOREGROUND_OK"),
        "the agent's command should have run in the foreground; output:\n{combined}"
    );
}

#[test]
fn interactive_fleet_refuses_and_names_single_agent_form() {
    let ws = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\nworktree = \".\"\n\
         [agents.second]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\nworktree = \".\"\n",
    );

    // No agent named + --interactive -> the whole fleet, no multiplexer.
    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up", "--interactive"])
        .current_dir(ws.path())
        .env("HOME", ws.path())
        .output()
        .expect("run gr spawn up --interactive");

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        !out.status.success(),
        "a fleet with no multiplexer must refuse, not launch; stderr:\n{stderr}"
    );
    assert!(
        stderr.contains("gr spawn up <agent> --interactive"),
        "the refusal must name the single-agent form; stderr:\n{stderr}"
    );
}

/// The texts promise the no-agent interactive form refuses — not "launches
/// whichever agent happens to lack a window". This is that promise's witness on
/// a box WITH tmux: `gr spawn up --interactive` (no agent) must refuse before
/// any registry or routing mutation, not fall through to launching the one
/// missing window in the foreground.
#[test]
fn interactive_without_agent_refuses_even_when_tmux_is_present() {
    let ws = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\nworktree = \".\"\n",
    );

    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up", "--interactive"])
        .current_dir(ws.path())
        .env("HOME", ws.path())
        .output()
        .expect("run gr spawn up --interactive with tmux present");

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "interactive with no agent named must refuse even when tmux exists; output:\n{combined}"
    );
    assert!(
        combined.contains("gr spawn up <agent> --interactive"),
        "the refusal must name the single-agent form; output:\n{combined}"
    );
    assert!(
        !combined.contains("in this terminal (cwd"),
        "the launch header must not print for a refusal; output:\n{combined}"
    );
}

/// Witness for the bash requirement the three texts must name: with NEITHER tmux
/// nor bash on PATH, `gr spawn up <agent>` refuses with a message naming bash —
/// and does not print the launch header first. This is the mutant witness for the
/// resolve-before-header check: dropping the mapping (so the header prints and
/// the bare `bash` spawn fails) leaves the marker absent and the header present,
/// which reds this test on the header assertion.
#[test]
fn no_bash_refuses_and_names_it_before_the_header() {
    let bin = TempDir::new().unwrap(); // empty: no bash, no tmux
    let ws = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\n\
         worktree = \".\"\nargs = [\"SHOULD_NOT_RUN\"]\n",
    );

    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up", "probe"])
        .current_dir(ws.path())
        .env("PATH", bin.path())
        .env("HOME", ws.path())
        .output()
        .expect("run gr spawn up probe with an empty PATH");

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "no bash on PATH must refuse, not launch; output:\n{combined}"
    );
    assert!(
        combined.contains("needs bash on PATH"),
        "the refusal must name bash; output:\n{combined}"
    );
    assert!(
        !combined.contains("in this terminal (cwd"),
        "the refusal must come BEFORE the launch header; output:\n{combined}"
    );
    assert!(
        !combined.contains("SHOULD_NOT_RUN"),
        "the agent's command must not have run; output:\n{combined}"
    );
}

/// Resolve an executable by name from the ambient test PATH.
fn resolve_on_path(name: &str) -> std::path::PathBuf {
    let path = std::env::var_os("PATH").expect("PATH is set in the test environment");
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if let Ok(meta) = fs::metadata(&candidate) {
            if meta.is_file() && meta.permissions().mode() & 0o111 != 0 {
                return candidate;
            }
        }
    }
    panic!("could not resolve `{name}` on the test PATH");
}

/// A directory holding a `bash` symlink and nothing else, used as the child's
/// entire PATH. bash is what `gr` execs to run the launch script and what the
/// stub's `/usr/bin/env bash` shebang resolves; excluding everything else means
/// `tmux_available()` returns false. Symlinking a single binary into an empty
/// dir is the only way to drop tmux on boxes where bash and tmux share one
/// directory (e.g. homebrew's bin).
fn tmuxless_bin_dir() -> TempDir {
    let bin = TempDir::new().unwrap();
    let bash = resolve_on_path("bash");
    std::os::unix::fs::symlink(bash, bin.path().join("bash")).unwrap();
    bin
}

/// Witness for the AUTOMATIC fallback: no `--interactive` flag, tmux absent from
/// PATH. A single named agent must foreground-launch (exit 0 + marker); the
/// whole fleet must refuse and name tmux's absence. This is the only test that
/// drives `interactive_mode = interactive || !tmux_ok` through the `!tmux_ok`
/// term — the two tests above always pass `--interactive`, so mutating the
/// expression to `interactive` alone leaves them GREEN. Under that mutation this
/// test REDS: the single-agent case falls through to `require_tmux()` and fails
/// with "tmux is required" (no marker, non-zero exit), and the fleet case's
/// stderr then reads "tmux is required for `gr spawn` but was not found", which
/// does NOT contain the exact substring "tmux was not found" asserted below.
#[test]
fn no_flag_no_tmux_falls_back_and_fleet_names_tmux_absence() {
    let bin = tmuxless_bin_dir();

    // Control: the constructed PATH genuinely cannot resolve tmux. If this were
    // to find tmux (e.g. an absolute-path leak), the assertions below would pass
    // for the wrong reason, so this must be checked first.
    let ctl = Command::new(bin.path().join("bash"))
        .args(["-c", "command -v tmux || true"])
        .env("PATH", bin.path())
        .output()
        .expect("run tmux-absence control");
    assert!(
        String::from_utf8_lossy(&ctl.stdout).trim().is_empty(),
        "control failed: tmux is resolvable on the constructed PATH:\n{}",
        String::from_utf8_lossy(&ctl.stdout)
    );

    // Single agent, NO --interactive: the automatic no-multiplexer fallback
    // foregrounds it.
    let single = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\n\
         worktree = \".\"\nargs = [\"AUTO_FOREGROUND_OK\"]\n",
    );
    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up", "probe"])
        .current_dir(single.path())
        .env("PATH", bin.path())
        .env("HOME", single.path())
        .output()
        .expect("run gr spawn up probe (no flag, no tmux)");
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        out.status.success(),
        "no-tmux single-agent launch should fall back to foreground and exit 0; got {:?}\n{combined}",
        out.status.code()
    );
    assert!(
        combined.contains("AUTO_FOREGROUND_OK"),
        "the agent's command should have run in the foreground; output:\n{combined}"
    );

    // Whole fleet, NO --interactive, no tmux: must refuse and name tmux absence.
    let fleet = write_gripspace(
        "[agents.probe]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\nworktree = \".\"\n\
         [agents.second]\nrole = \"worker\"\ntool = \"echotool\"\nmodel = \"\"\nworktree = \".\"\n",
    );
    let out = Command::cargo_bin("gr")
        .unwrap()
        .args(["spawn", "up"])
        .current_dir(fleet.path())
        .env("PATH", bin.path())
        .env("HOME", fleet.path())
        .output()
        .expect("run gr spawn up (no flag, no tmux, whole fleet)");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        !out.status.success(),
        "a fleet with no multiplexer must refuse, not launch; stderr:\n{stderr}"
    );
    assert!(
        stderr.contains("tmux was not found"),
        "the refusal must name tmux's absence; stderr:\n{stderr}"
    );
}
