//! The `gr` command, Rust half: pick gr1 or gr2 by the nearest workspace marker and run it.
//!
//! The rule is a table (`conformance/gr-resolver/cases.toml`) and this file only applies it, the same table the
//! Python half runs. It is not wired to the `gr` binary yet: the entry point ships with the binary rename, so
//! nothing a user has installed changes. It is built and run as `examples/gr_resolver.rs`.
//!
//! * the nearest marker wins: a `.gitgrip` entry means gr1; `grip.toml` beside a `.git`, or a workspace spec under
//!   the grip directory, means gr2; no marker means gr2;
//! * a directory holding both goes to gr1, with one stderr line saying it also holds a gr2 workspace;
//! * the chosen half is run from PATH with `GR_RESOLVED` set; a second resolution refuses (exit 70); a half that
//!   is not installed refuses in one sentence (exit 69);
//! * `GR2_QUIET_CONTEXT` silences only the informational line, never a refusal;
//! * invoked under the name of a half (`gr1` or `gr2`), it runs that half and resolves nothing;
//! * `--which` names another `gr` on PATH that is not this one.

use std::env;
use std::path::{Path, PathBuf};

pub const EXIT_LOOP: i32 = 70;
pub const EXIT_MISSING_HALF: i32 = 69;
const GR1_MARKER: &str = ".gitgrip";
const GR2_ROOT_FILE: &str = "grip.toml";
const GRIP_DIR: &str = ".grip";
const SPEC_FILE: &str = "workspace_spec.toml";

/// Which half answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Half {
    Gr1,
    Gr2,
}

impl Half {
    pub fn name(self) -> &'static str {
        match self {
            Half::Gr1 => "gr1",
            Half::Gr2 => "gr2",
        }
    }
}

/// The answer for one directory: the half, the deciding marker (None when no marker was found), and a note.
#[derive(Debug, PartialEq, Eq)]
pub struct Resolution {
    pub half: Half,
    pub marker: Option<PathBuf>,
    pub note: &'static str,
}

fn is_gr2(dir: &Path) -> bool {
    (dir.join(GR2_ROOT_FILE).is_file() && dir.join(".git").exists())
        || dir.join(GRIP_DIR).join(SPEC_FILE).is_file()
}

fn is_gr1(dir: &Path) -> bool {
    dir.join(GR1_MARKER).exists()
}

/// Resolve the nearest marker at or above `cwd`.
pub fn resolve(cwd: &Path) -> Resolution {
    let mut dir = cwd.to_path_buf();
    loop {
        if is_gr1(&dir) {
            let note = if is_gr2(&dir) {
                "; this root also holds a gr2 workspace, use gr2 for it"
            } else {
                ""
            };
            return Resolution {
                half: Half::Gr1,
                marker: Some(dir.join(GR1_MARKER)),
                note,
            };
        }
        if is_gr2(&dir) {
            return Resolution {
                half: Half::Gr2,
                marker: Some(dir),
                note: "",
            };
        }
        match dir.parent() {
            Some(parent) if parent != dir => dir = parent.to_path_buf(),
            _ => {
                return Resolution {
                    half: Half::Gr2,
                    marker: None,
                    note: "",
                }
            }
        }
    }
}

fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        path.is_file()
            && path
                .metadata()
                .map(|m| m.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

fn path_entries() -> Vec<PathBuf> {
    env::var_os("PATH")
        .map(|p| env::split_paths(&p).collect())
        .unwrap_or_default()
}

fn which(name: &str) -> Option<PathBuf> {
    path_entries()
        .into_iter()
        .map(|d| d.join(name))
        .find(|p| is_executable(p))
}

/// Every executable named `gr` on PATH that is not this command.
fn other_grs_on_path(own: &Path) -> Vec<PathBuf> {
    let mut seen: Vec<PathBuf> = Vec::new();
    let mut found = Vec::new();
    for entry in path_entries() {
        let candidate = entry.join("gr");
        if is_executable(&candidate) {
            let real = candidate
                .canonicalize()
                .unwrap_or_else(|_| candidate.clone());
            if real != own && !seen.contains(&real) {
                seen.push(real);
                found.push(candidate);
            }
        }
    }
    found
}

fn exec(found: &Path, name: &str, args: &[String], resolved: Option<&str>) -> i32 {
    let mut command = std::process::Command::new(found);
    command.args(args);
    if let Some(value) = resolved {
        command.env("GR_RESOLVED", value);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.arg0(name);
        let error = command.exec();
        eprintln!("gr: could not run {name}: {error}");
        1
    }
    #[cfg(not(unix))]
    {
        let _ = name;
        match command.status() {
            Ok(status) => status.code().unwrap_or(1),
            Err(error) => {
                eprintln!("gr: could not run {name}: {error}");
                1
            }
        }
    }
}

/// What answers one invocation, decided ONCE and read by both `--which` and the run itself, so the report can never
/// name a different half than the one that runs.
#[derive(Debug, PartialEq, Eq)]
enum Target {
    /// Exec this path as this half; `fallback` = gr2 was asked for (no marker) but is absent, so gr1 answers.
    Exec {
        half: Half,
        path: PathBuf,
        fallback: bool,
    },
    /// This process is gr1 and answers itself (the `gr` binary only).
    InProcess { fallback: bool },
    /// The half that was asked for is not installed: refuse.
    Missing(Half),
}

/// `gr1_here` is true only in the gr1 binary, which can answer gr1 itself instead of looking for one on PATH.
fn target(resolution: &Resolution, gr1_here: bool) -> Target {
    let gr1 = |fallback: bool| {
        if gr1_here {
            Some(Target::InProcess { fallback })
        } else {
            which("gr1").map(|path| Target::Exec {
                half: Half::Gr1,
                path,
                fallback,
            })
        }
    };
    match resolution.half {
        Half::Gr1 => gr1(false).unwrap_or(Target::Missing(Half::Gr1)),
        Half::Gr2 => match which("gr2") {
            Some(path) => Target::Exec {
                half: Half::Gr2,
                path,
                fallback: false,
            },
            // No marker asked for gr2 and gr2 is absent: a brew install carries gr1 only, so `gr --help` outside a
            // workspace runs gr1 with one line naming the install, instead of refusing. A gr2 MARKER with gr2
            // missing still refuses: that directory asked for gr2.
            None if resolution.marker.is_none() => gr1(true).unwrap_or(Target::Missing(Half::Gr2)),
            None => Target::Missing(Half::Gr2),
        },
    }
}

/// `--which`: `<half> <marker> <binary>` for the half that WILL answer (after any fallback), the binary being this
/// executable when it answers in-process.
fn report_which(resolution: &Resolution, target: &Target) {
    let own = env::current_exe()
        .and_then(|p| p.canonicalize())
        .unwrap_or_default();
    if let Some(other) = other_grs_on_path(&own).first() {
        eprintln!(
            "gr: another `gr` is on PATH: {}; it may answer instead of this one",
            other.display()
        );
    }
    let marker = resolution
        .marker
        .as_ref()
        .map(|m| m.display().to_string())
        .unwrap_or_else(|| "none".into());
    let (half, binary) = match target {
        Target::Exec { half, path, .. } => (*half, path.display().to_string()),
        Target::InProcess { .. } => (Half::Gr1, own.display().to_string()),
        Target::Missing(half) => (*half, "missing".to_string()),
    };
    println!("{} {marker} {binary}", half.name());
}

fn install_line(quiet: bool) {
    if !quiet {
        eprintln!("gr: gr2 not installed; running gr1. pip install --pre gitgrip for gr2");
    }
}

/// Run the resolver for one invocation. `invoked_as` is the command name it was run under; `args` excludes it.
/// Returns the exit code when it returns at all: a resolved half replaces the process on unix.
pub fn run(invoked_as: &str, args: &[String]) -> i32 {
    if invoked_as == "gr1" || invoked_as == "gr2" {
        return match which(invoked_as) {
            Some(found) => exec(&found, invoked_as, args, None),
            None => {
                eprintln!("gr: this needs {invoked_as}, which is not installed");
                EXIT_MISSING_HALF
            }
        };
    }
    if let Some(resolved) = env::var("GR_RESOLVED").ok().filter(|v| !v.is_empty()) {
        eprintln!("gr: refusing to resolve twice (GR_RESOLVED={resolved})");
        return EXIT_LOOP;
    }
    let cwd = current_dir();
    let resolution = resolve(&cwd);
    let chosen = target(&resolution, false);
    if args.first().map(String::as_str) == Some("--which") {
        report_which(&resolution, &chosen);
        return 0;
    }
    let quiet = quiet_context();
    context_line(&resolution, &cwd, quiet);
    run_target(chosen, args, quiet).unwrap_or(EXIT_MISSING_HALF)
}

/// Act on a decided target. `None` means "answer in-process" (only the gr1 binary decides that).
fn run_target(chosen: Target, args: &[String], quiet: bool) -> Option<i32> {
    match chosen {
        Target::Exec {
            half,
            path,
            fallback,
        } => {
            if fallback {
                install_line(quiet);
            }
            Some(exec(&path, half.name(), args, Some(half.name())))
        }
        Target::InProcess { fallback } => {
            if fallback {
                install_line(quiet);
            }
            None
        }
        Target::Missing(half) => {
            eprintln!("gr: this needs {}, which is not installed", half.name());
            Some(EXIT_MISSING_HALF)
        }
    }
}

/// A [`std::process::Command`] that runs this binary as gr1, for re-running itself. A command this process builds for
/// itself is gr1's by construction, but a `gr`-named executable resolves again on every run (a resolver line in the
/// captured output; with another working directory, possibly gr2). On unix: this executable with `gr1` as its program
/// name, which `entry` does not resolve, and which is always the same version as the caller. Elsewhere there is no
/// program name to set: the sibling `gr1` beside this executable when present, else this executable, which then still
/// resolves under its own name.
pub fn gr1_self_command() -> std::io::Result<std::process::Command> {
    let exe = env::current_exe()?;
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new(&exe);
        command.arg0("gr1");
        Ok(command)
    }
    #[cfg(not(unix))]
    {
        let sibling = exe.with_file_name(format!("gr1{}", env::consts::EXE_SUFFIX));
        let gr_named = exe.file_stem().and_then(|s| s.to_str()) == Some("gr");
        Ok(std::process::Command::new(
            if gr_named && sibling.is_file() {
                sibling
            } else {
                exe
            },
        ))
    }
}

fn quiet_context() -> bool {
    env::var("GR2_QUIET_CONTEXT")
        .map(|v| !v.is_empty())
        .unwrap_or(false)
}

fn current_dir() -> PathBuf {
    env::current_dir()
        .and_then(|d| d.canonicalize())
        .unwrap_or_else(|_| PathBuf::from("."))
}

/// The one informational line: the deciding marker sits above the current directory, or the root also holds a gr2
/// workspace. `GR2_QUIET_CONTEXT` silences it; it never silences a refusal.
fn context_line(resolution: &Resolution, cwd: &Path, quiet: bool) {
    let outside = match &resolution.marker {
        Some(marker) => marker.parent() != Some(cwd) && marker != cwd,
        None => false,
    };
    if !quiet && (!resolution.note.is_empty() || outside) {
        let where_ = resolution
            .marker
            .as_ref()
            .map(|m| m.display().to_string())
            .unwrap_or_else(|| "no workspace marker".into());
        eprintln!(
            "gr: {} ({where_}{})",
            resolution.half.name(),
            resolution.note
        );
    }
}

/// The `gr1` binary's first step. Invoked as `gr`, it resolves: a gr1 answer runs here, in-process (this binary IS
/// gr1, so it needs no `gr1` on PATH), and a gr2 answer execs gr2. Invoked as `gr2` it runs gr2. Under any other
/// name (`gr1`, `gitgrip`) it resolves nothing and returns `None`, after clearing `GR_RESOLVED`: the marker only
/// stops the one exec it guards, and a half that kept it would hand it to everything it starts (hooks, `gr spawn`'s
/// panes), where every later `gr` would refuse to resolve twice.
pub fn entry() -> Option<i32> {
    let mut argv = env::args();
    let argv0 = argv.next().unwrap_or_default();
    let name = Path::new(&argv0)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or_default()
        .to_string();
    if name == "gr2" {
        // Under gr2's name this binary is a launcher for gr2, never gr1 (the table's explicit-name rows).
        let rest: Vec<String> = argv.collect();
        return Some(run("gr2", &rest));
    }
    if name == "gr" {
        let rest: Vec<String> = argv.collect();
        let resolving_twice = env::var("GR_RESOLVED")
            .map(|v| !v.is_empty())
            .unwrap_or(false);
        if resolving_twice {
            return Some(run("gr", &rest));
        }
        let cwd = current_dir();
        let resolution = resolve(&cwd);
        let chosen = target(&resolution, true);
        if rest.first().map(String::as_str) == Some("--which") {
            report_which(&resolution, &chosen);
            return Some(0);
        }
        let quiet = quiet_context();
        context_line(&resolution, &cwd, quiet);
        if let Some(code) = run_target(chosen, &rest, quiet) {
            return Some(code);
        }
    }
    // Called before any thread starts (main is not async), so mutating the environment is safe here.
    env::remove_var("GR_RESOLVED");
    None
}
