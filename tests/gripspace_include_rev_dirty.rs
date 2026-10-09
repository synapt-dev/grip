//! Regression: an included gripspace carrying `rev:` plus ANY local
//! change in its space clone must NOT silently drop every included repo.
//!
//! *** THE DEFECT (v1.0.1..1.5.0, Layne 2026-09-08: "the latest gr is broken.
//! it doesn't show the included gripspace repos"). ***
//!
//! Trigger: a `gripspaces:` include with a `rev:`, plus ANY local change inside
//! that include's clone under `.gitgrip/spaces/<name>/` (an untracked file is
//! enough). Mechanism, three parts, all on the resolve path:
//!   1. `checkout_rev` ran `git status --porcelain` and refused on a non-empty
//!      result — untracked files included, and even when HEAD was already the
//!      target rev.
//!   2. `resolve_all_gripspaces` `take()`n the gripspaces list BEFORE it
//!      succeeded, so the first error left the manifest with neither its
//!      declaration nor any merged repo.
//!   3. `dispatch.rs` called the resolver as `let _ = …` — the error was
//!      discarded. Every command then saw an overlay-only manifest, silently.
//!
//! These witnesses drive the real binary end to end: a status/sync after a
//! stray file in the space clone must still show the included repos, and a
//! GENUINE resolve failure must WARN (never silently drop) and keep the
//! declaration visible.

use assert_cmd::Command;
use std::path::Path;
use std::process::Command as StdCommand;
use tempfile::TempDir;

fn git(dir: &Path, args: &[&str]) {
    let out = StdCommand::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t")
        .output()
        .unwrap_or_else(|e| panic!("git {args:?} failed to spawn: {e}"));
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

fn gr() -> Command {
    let mut c = Command::cargo_bin("gr1").expect("gr binary");
    c.env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t");
    c
}

/// A bare repo at `root/<name>.git` seeded with one commit on `branch` carrying
/// `<file>=<content>`. Returns its file:// URL (through the production helper).
fn seeded_bare(root: &Path, name: &str, branch: &str, file: &str, content: &str) -> String {
    git(
        root,
        &[
            "init",
            "--bare",
            &format!("--initial-branch={branch}"),
            &format!("{name}.git"),
        ],
    );
    let bare = root.join(format!("{name}.git"));
    let url = gitgrip::core::gripspace::path_to_file_url(&bare);
    let w = root.join(format!("seed-{name}"));
    std::fs::create_dir_all(&w).unwrap();
    git(&w, &["init", &format!("--initial-branch={branch}")]);
    std::fs::write(w.join(file), content).unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "seed"]);
    git(&w, &["push", &url, branch]);
    url
}

/// A bare gripspace repo whose `main` carries `gripspace.yml = body`.
fn gripspace_bare(root: &Path, name: &str, body: &str) -> String {
    git(
        root,
        &[
            "init",
            "--bare",
            "--initial-branch=main",
            &format!("{name}.git"),
        ],
    );
    let bare = root.join(format!("{name}.git"));
    let url = gitgrip::core::gripspace::path_to_file_url(&bare);
    let w = root.join(format!("seed-{name}"));
    std::fs::create_dir_all(&w).unwrap();
    git(&w, &["init", "--initial-branch=main"]);
    std::fs::write(w.join("gripspace.yml"), body).unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "manifest"]);
    git(&w, &["push", &url, "main"]);
    url
}

fn workspace_dir(parent: &Path) -> std::path::PathBuf {
    std::fs::read_dir(parent)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| p.is_dir() && p.join(".gitgrip").exists())
        .expect("gr init produced a workspace directory")
}

/// The included gripspace's clone under `<ws>/.gitgrip/spaces/`.
///
/// `spaces/` also holds `main` (the workspace's own manifest space, a git repo)
/// and `local` (a non-repo staging dir). The include's clone is the OTHER
/// directory that is itself a git repo — the one `checkout_rev` operates on and
/// where a stray file must land to reproduce the defect. Requiring `.git`
/// excludes `local`; excluding `main` leaves exactly the include.
fn included_space_clone(ws: &Path) -> std::path::PathBuf {
    let spaces = ws.join(".gitgrip").join("spaces");
    std::fs::read_dir(&spaces)
        .unwrap_or_else(|e| panic!("spaces dir {spaces:?} unreadable: {e}"))
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| {
            p.is_dir()
                && p.file_name().and_then(|n| n.to_str()) != Some("main")
                && p.join(".git").exists()
        })
        .unwrap_or_else(|| panic!("no included-gripspace clone (dir with .git) under {spaces:?}"))
}

fn stdout_of(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stdout).to_string()
}
fn stderr_of(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).to_string()
}

/// Build the reported shape: a base gripspace that CONTRIBUTES two repos, an
/// overlay that includes it at `rev: main` and adds one local repo. Returns the
/// overlay URL to `gr init`.
fn build_overlay_including_base_with_rev(root: &Path) -> String {
    let ra = seeded_bare(root, "ra", "main", "A.md", "included-A\n");
    let rb = seeded_bare(root, "rb", "main", "B.md", "included-B\n");
    let base = gripspace_bare(
        root,
        "base",
        &format!(
            "version: 2\nmanifest:\n  url: \"\"\nrepos:\n  \
             a:\n    url: {ra}\n    path: ./a\n  \
             b:\n    url: {rb}\n    path: ./b\n"
        ),
    );
    let rc = seeded_bare(root, "rc", "main", "C.md", "local-C\n");
    gripspace_bare(
        root,
        "overlay",
        &format!(
            "version: 2\nmanifest:\n  url: \"\"\ngripspaces:\n  \
             - url: {base}\n    rev: main\nrepos:\n  \
             c:\n    url: {rc}\n    path: ./c\n"
        ),
    )
}

/// Primary regression (Fix 3): an UNTRACKED file in the include's space clone
/// must not drop the included repos. `gr status` after the stray touch still
/// shows both included repos and the Gripspace section reports OK.
#[test]
fn untracked_file_in_space_clone_does_not_drop_included_repos() {
    let tmp = TempDir::new().unwrap();
    let overlay = build_overlay_including_base_with_rev(tmp.path());
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();

    gr().args(["init", &overlay])
        .current_dir(&ws_parent)
        .assert()
        .success();
    let ws = workspace_dir(&ws_parent);
    gr().args(["sync"]).current_dir(&ws).assert().success();

    // Control: clean, the include composed — two included repos + one local =
    // 3/3 cloned (status reports the merged repo count).
    let clean = gr().args(["status"]).current_dir(&ws).output().unwrap();
    let clean_out = stdout_of(&clean);
    assert!(
        clean_out.contains("3/3 cloned"),
        "control: the two included repos + the local repo must be present when the \
         space clone is clean; got:\n{clean_out}"
    );

    // Trigger: an untracked stray file inside the include's space clone.
    let clone = included_space_clone(&ws);
    std::fs::write(clone.join("stray.txt"), "untracked\n").unwrap();

    // The defect: 1.0.1..1.5.0 drop to the overlay-only view here (1/1 cloned,
    // just the local repo), silently.
    let out = gr().args(["status"]).current_dir(&ws).output().unwrap();
    let so = stdout_of(&out);
    assert!(
        so.contains("3/3 cloned"),
        "REGRESSION (included-gripspace drop): an untracked file in the space clone dropped the \
         included repos (expected merged count 3/3). status stdout:\n{so}\nstderr:\n{}",
        stderr_of(&out)
    );
}

/// Fix 1 + Fix 2 witness: a GENUINE resolve failure (a tracked modification in
/// the clone while HEAD is behind origin/<rev>) must WARN (never silent) and
/// must keep the gripspaces declaration visible in `gr status` (the list is no
/// longer take()n away before the resolver succeeds).
#[test]
fn genuine_resolve_failure_warns_and_keeps_declaration() {
    let tmp = TempDir::new().unwrap();
    let ra = seeded_bare(tmp.path(), "ra", "main", "A.md", "included-A\n");
    // base gripspace with TWO commits on main so the clone can sit one behind.
    let base = {
        git(
            tmp.path(),
            &["init", "--bare", "--initial-branch=main", "base.git"],
        );
        let bare = tmp.path().join("base.git");
        let url = gitgrip::core::gripspace::path_to_file_url(&bare);
        let w = tmp.path().join("seed-base");
        std::fs::create_dir_all(&w).unwrap();
        git(&w, &["init", "--initial-branch=main"]);
        let body = format!(
            "version: 2\nmanifest:\n  url: \"\"\nrepos:\n  a:\n    url: {ra}\n    path: ./a\n"
        );
        std::fs::write(w.join("gripspace.yml"), &body).unwrap();
        git(&w, &["add", "-A"]);
        git(&w, &["commit", "-m", "c1"]);
        // second commit (a comment bump) — gives the clone a HEAD~1 to sit on.
        std::fs::write(w.join("gripspace.yml"), format!("{body}# c2\n")).unwrap();
        git(&w, &["add", "-A"]);
        git(&w, &["commit", "-m", "c2"]);
        git(&w, &["push", &url, "main"]);
        url
    };
    let rc = seeded_bare(tmp.path(), "rc", "main", "C.md", "local-C\n");
    let overlay = gripspace_bare(
        tmp.path(),
        "overlay",
        &format!(
            "version: 2\nmanifest:\n  url: \"\"\ngripspaces:\n  \
             - url: {base}\n    rev: main\nrepos:\n  c:\n    url: {rc}\n    path: ./c\n"
        ),
    );
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();
    gr().args(["init", &overlay])
        .current_dir(&ws_parent)
        .assert()
        .success();
    let ws = workspace_dir(&ws_parent);
    gr().args(["sync"]).current_dir(&ws).assert().success();

    // Move the clone one commit behind origin/main AND leave a TRACKED change,
    // so checkout_rev's advance is both needed and legitimately blocked.
    let clone = included_space_clone(&ws);
    git(&clone, &["reset", "--hard", "HEAD~1"]);
    std::fs::write(
        clone.join("gripspace.yml"),
        "version: 2\n# locally edited\n",
    )
    .unwrap();

    let out = gr().args(["status"]).current_dir(&ws).output().unwrap();
    let se = stderr_of(&out);
    let so = stdout_of(&out);
    // Fix 1: the resolver error is surfaced, not swallowed.
    assert!(
        se.contains("could not resolve gripspace includes"),
        "Fix 1: a genuine resolve failure must warn on stderr. stderr:\n{se}\nstdout:\n{so}"
    );
    // Fix 3: the refusal names the clone path and that it is a tracked change.
    assert!(
        se.contains("local (tracked) changes"),
        "Fix 3: the refusal must name tracked changes. stderr:\n{se}"
    );
    // Fix 2: the declaration is not erased — the Gripspace section still prints.
    assert!(
        so.contains("Gripspace"),
        "Fix 2: the gripspaces declaration must survive a resolve error \
         (status still shows the Gripspace section). stdout:\n{so}"
    );
}

/// R2 v3 (warn-at-rev): with the space clone AT the pinned rev, a TRACKED edit
/// to its manifest is HONORED (repos never dropped) but WARNED — gr is
/// resolving from the local content, not the pin. Untracked at-rev stays silent
/// (covered above). This path is the one `status` actually reaches, because
/// `status` never fetches, so the clone stays at the rev; the refuse-on-advance
/// path needs a fetch to move origin/<rev> ahead. Mutation: removing the
/// eprintln in checkout_rev's at-rev branch drops the warning and reds this.
#[test]
fn at_rev_tracked_edit_warns_and_keeps_repos() {
    let tmp = TempDir::new().unwrap();
    let overlay = build_overlay_including_base_with_rev(tmp.path());
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();
    gr().args(["init", &overlay])
        .current_dir(&ws_parent)
        .assert()
        .success();
    let ws = workspace_dir(&ws_parent);
    gr().args(["sync"]).current_dir(&ws).assert().success();

    // The clone is at origin/main. Modify a TRACKED file (append a comment):
    // HEAD stays at the rev, the working tree is tracked-dirty.
    let clone = included_space_clone(&ws);
    let gsyml = clone.join("gripspace.yml");
    let mut body = std::fs::read_to_string(&gsyml).unwrap();
    body.push_str("# local edit\n");
    std::fs::write(&gsyml, body).unwrap();

    let out = gr().args(["status"]).current_dir(&ws).output().unwrap();
    let se = stderr_of(&out);
    let so = stdout_of(&out);
    // Warned, naming the rev and the tracked file.
    assert!(
        se.contains("local (tracked) changes")
            && se.contains("'main'")
            && se.contains("gripspace.yml"),
        "at-rev tracked edit must warn, naming the rev and the file. stderr:\n{se}"
    );
    // Repos never dropped: the include still resolves — 3/3 cloned.
    assert!(
        so.contains("3/3 cloned"),
        "at-rev tracked edit must NOT drop the included repos. stdout:\n{so}"
    );
}
