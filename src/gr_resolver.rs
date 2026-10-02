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
    let cwd = env::current_dir()
        .and_then(|d| d.canonicalize())
        .unwrap_or_else(|_| PathBuf::from("."));
    let resolution = resolve(&cwd);
    let kind = resolution.half.name();
    let found = which(kind);
    if args.first().map(String::as_str) == Some("--which") {
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
        let binary = found
            .as_ref()
            .map(|f| f.display().to_string())
            .unwrap_or_else(|| "missing".into());
        println!("{kind} {marker} {binary}");
        return 0;
    }
    let quiet = env::var("GR2_QUIET_CONTEXT")
        .map(|v| !v.is_empty())
        .unwrap_or(false);
    let outside = match &resolution.marker {
        Some(marker) => marker.parent() != Some(cwd.as_path()) && marker != &cwd,
        None => false,
    };
    if !quiet && (!resolution.note.is_empty() || outside) {
        let where_ = resolution
            .marker
            .as_ref()
            .map(|m| m.display().to_string())
            .unwrap_or_else(|| "no workspace marker".into());
        eprintln!("gr: {kind} ({where_}{})", resolution.note);
    }
    match found {
        Some(path) => exec(&path, kind, args, Some(kind)),
        None => {
            eprintln!("gr: this needs {kind}, which is not installed");
            EXIT_MISSING_HALF
        }
    }
}
