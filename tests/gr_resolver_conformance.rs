//! Run the shared `gr` resolver table against the Rust resolver, built as `examples/gr_resolver.rs`.
//!
//! No rule lives here: the runner builds each row's layout and stub `gr1`/`gr2` binaries, runs the resolver as a
//! separate process under the command name the row asks for, and compares the tool, the stderr line count, the
//! `--which` text and the exit code. A skipped row is a red row: the run prints `ran N of M rows` and fails unless
//! N equals M minus the rows the table says are not for this half. The stubs are shell scripts, so the whole run
//! is Unix-only and says so on other platforms instead of passing silently.
//!
//! Needs the example built: `cargo build --example gr_resolver` first (a plain `cargo test` builds examples too).

#[cfg(not(unix))]
#[test]
fn the_gr_resolver_table_is_unix_only_here() {
    // The stub gr1 and gr2 are shell scripts, so no row can run on this platform. Say so rather than pass silently.
    println!("ran 0 rows: the whole table is unix_only on this platform (stubs are shell scripts)");
}

/// Everything that needs a shell, a link or a mode bit is inside this module, so on another platform the file
/// compiles to the one test above and nothing else.
#[cfg(unix)]
mod table {
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::path::{Path, PathBuf};
    use std::process::Command;

    const CONSUMER: &str = "rust";
    const MARKERS: [&str; 3] = [".gitgrip", "grip.toml", ".grip/workspace_spec.toml"];

    fn example_binary() -> PathBuf {
        let exe = std::env::current_exe().expect("test executable path");
        let debug_dir = exe
            .parent()
            .and_then(Path::parent)
            .expect("target profile directory");
        let path = debug_dir.join("examples").join("gr_resolver");
        assert!(
            path.exists(),
            "{} is not built; run `cargo build --example gr_resolver` first (a plain `cargo test` builds examples too)",
            path.display()
        );
        path
    }

    fn write_executable(path: &Path, text: &str) {
        fs::write(path, text).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn stub(name: &str) -> String {
        format!(
            "#!/bin/sh\nprintf \"STUB {name}\"; for a in \"$@\"; do printf \" %s\" \"$a\"; done; printf \"\\n\"; \
             printf \"GR_RESOLVED=%s\\n\" \"${{GR_RESOLVED:-}}\"; exit \"${{STUB_EXIT:-0}}\"\n"
        )
    }

    fn materialize(root: &Path, layout: &[String]) {
        for entry in layout {
            if let Some((link, target)) = entry.split_once(" -> ") {
                fs::create_dir_all((root.join(link)).parent().unwrap()).unwrap();
                symlink(root.join(target), root.join(link)).unwrap();
            } else if entry.ends_with('/') {
                fs::create_dir_all(root.join(entry)).unwrap();
            } else {
                fs::create_dir_all(root.join(entry).parent().unwrap()).unwrap();
                fs::write(root.join(entry), "").unwrap();
            }
        }
    }

    /// Bytes held by regular files under `dir`; links are not followed, so a link to a large binary counts as itself.
    fn disk_bytes(dir: &Path) -> u64 {
        fs::read_dir(dir)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|e| match e.path().symlink_metadata() {
                        Ok(m) if m.is_dir() => disk_bytes(&e.path()),
                        Ok(m) => m.len(),
                        Err(_) => 0,
                    })
                    .sum()
            })
            .unwrap_or(0)
    }

    fn which_norm(out: &str, real_root: &Path) -> String {
        let parts: Vec<&str> = out.trim().splitn(3, ' ').collect();
        if parts.len() != 3 {
            return out.trim().to_string();
        }
        let rel = if parts[1] == "none" {
            "none".to_string()
        } else {
            let rel = Path::new(parts[1])
                .strip_prefix(real_root)
                .map(|p| p.to_path_buf())
                .unwrap_or_default();
            if rel.as_os_str().is_empty() {
                ".".to_string()
            } else {
                rel.display().to_string()
            }
        };
        let binary = Path::new(parts[2])
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        format!("{} {} {}", parts[0], rel, binary)
    }

    fn tool_of(stdout: &str) -> Option<String> {
        let first = stdout.lines().next().unwrap_or("");
        let first = first.strip_prefix("STUB ").unwrap_or(first);
        let word: String = first.chars().take_while(|c| c.is_alphanumeric()).collect();
        if word == "gr1" || word == "gr2" {
            Some(word)
        } else {
            None
        }
    }

    fn strings(value: Option<&toml::Value>) -> Vec<String> {
        value
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    }

    #[cfg(unix)]
    #[test]
    fn the_gr_resolver_table_runs_green_against_the_rust_resolver() {
        run_table(&example_binary(), false);
    }

    #[cfg(unix)]
    fn run_table(binary: &Path, real_gr: bool) {
        let table_path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("conformance/gr-resolver/cases.toml");
        let table: toml::Value = fs::read_to_string(&table_path).unwrap().parse().unwrap();
        let cases = table["case"].as_array().unwrap();
        assert_eq!(
            table["count"].as_integer().unwrap() as usize,
            cases.len(),
            "table count disagrees with its rows"
        );
        let mut ids: Vec<&str> = cases.iter().map(|c| c["id"].as_str().unwrap()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), cases.len(), "duplicate row id");

        println!("resolver under test: {}", binary.display());
        let base = tempfile::tempdir().unwrap();
        let base_path = base.path().canonicalize().unwrap();
        let mut ancestor: Option<&Path> = Some(&base_path);
        while let Some(dir) = ancestor {
            for marker in MARKERS {
                assert!(
                    !dir.join(marker).exists(),
                    "HERMETIC GUARD: {} exists above the scratch root",
                    dir.join(marker).display()
                );
            }
            ancestor = dir.parent();
        }

        let (mut ran, mut red, mut excluded) = (0usize, Vec::new(), Vec::new());
        let (mut in_process, mut in_process_named) = (0usize, Vec::new());
        for (i, c) in cases.iter().enumerate() {
            let id = c["id"].as_str().unwrap();
            let applies = c
                .get("applies_to")
                .map(|v| strings(Some(v)))
                .unwrap_or_else(|| vec!["oracle".into(), "rust".into(), "python".into()]);
            if !applies.iter().any(|a| a == CONSUMER) {
                excluded.push(format!("{id} (applies_to {applies:?})"));
                continue;
            }
            let root = base_path.join(format!("r{i}"));
            fs::create_dir_all(&root).unwrap();
            let real_root = root.canonicalize().unwrap();
            let bin_dir = base_path.join(format!("bin{i}"));
            fs::create_dir_all(&bin_dir).unwrap();
            let installed = c
                .get("installed")
                .map(|v| strings(Some(v)))
                .unwrap_or_else(|| vec!["gr1".into(), "gr2".into()]);
            for name in &installed {
                write_executable(&bin_dir.join(name), &stub(name));
            }
            materialize(&root, &strings(c.get("layout")));
            let mut path = bin_dir.display().to_string();
            if c.get("expect_which_stderr").is_some() {
                let decoy = base_path.join(format!("decoy{i}"));
                fs::create_dir_all(&decoy).unwrap();
                write_executable(&decoy.join("gr"), "#!/bin/sh\necho decoy\n");
                path = format!("{path}:{}", decoy.display());
            }
            path.push_str(":/usr/bin:/bin");
            let home = base_path.join(format!("home{i}"));
            fs::create_dir_all(&home).unwrap();
            let launch_dir = base_path.join(format!("launch{i}"));
            fs::create_dir_all(&launch_dir).unwrap();
            let name = c.get("argv0").and_then(|v| v.as_str()).unwrap_or("gr");
            let exe = launch_dir.join(name);
            // A link, not a copy: the example binary is large and there is one launcher per row. argv[0] keeps the
            // name the row asks for, which is all the resolver reads to tell `gr` from `gr1` and `gr2`.
            symlink(binary, &exe).unwrap();
            let cwd = root.join(c["cwd"].as_str().unwrap());

            let run = |args: &[&str]| {
                let mut command = Command::new(&exe);
                command
                    .args(args)
                    .current_dir(&cwd)
                    .env_clear()
                    .env("HOME", &home)
                    .env("PATH", &path);
                if let Some(env) = c.get("env").and_then(|v| v.as_table()) {
                    for (k, v) in env {
                        command.env(k, v.as_str().unwrap());
                    }
                }
                command.output().unwrap()
            };
            let expect_tool = c["expect_tool"].as_str().unwrap();
            let gr1_here = real_gr && expect_tool == "gr1";
            let mut args: Vec<String> = c
                .get("args")
                .map(|v| strings(Some(v)))
                .unwrap_or_else(|| vec!["--version".into()]);
            if gr1_here {
                in_process += 1;
                if args != ["--version"] || c.get("expect_stdout").is_some() {
                    in_process_named.push(id.to_string());
                }
                args = vec!["--version".into()];
            }
            let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
            let out = run(&arg_refs);
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            let lines = stderr.lines().filter(|l| !l.trim().is_empty()).count();
            let rc = out.status.code().unwrap_or(-1);
            let mut fails: Vec<String> = Vec::new();
            let expect_rc = c.get("expect_rc").and_then(|v| v.as_integer());
            if expect_tool == "refuse" {
                if Some(rc as i64) != expect_rc {
                    fails.push(format!("rc {rc} != {expect_rc:?}"));
                }
            } else if gr1_here {
                let first = stdout.lines().next().unwrap_or("");
                let version_line = (first.starts_with("gr ") || first.starts_with("gr1 "))
                    && first
                        .split_whitespace()
                        .nth(1)
                        .is_some_and(|v| v.starts_with('1'));
                if !version_line || stdout.contains("STUB") {
                    fails.push(format!(
                        "gr1 did not answer in-process (stdout {:?}, stderr {:?})",
                        stdout.trim(),
                        stderr.trim()
                    ));
                }
                let want_lines = c["expect_lines"].as_integer().unwrap() as usize;
                if lines != want_lines {
                    fails.push(format!("stderr lines {lines} != {want_lines}"));
                }
                if rc != 0 {
                    fails.push(format!("rc {rc} != 0"));
                }
            } else {
                let got = tool_of(&stdout);
                if got.as_deref() != Some(expect_tool) {
                    fails.push(format!(
                        "tool {got:?} != {expect_tool:?} (rc {rc}, stderr {:?})",
                        stderr.trim()
                    ));
                }
                let want_lines = c["expect_lines"].as_integer().unwrap() as usize;
                if lines != want_lines {
                    fails.push(format!("stderr lines {lines} != {want_lines}"));
                }
                if rc as i64 != expect_rc.unwrap_or(0) {
                    fails.push(format!("rc {rc} != {}", expect_rc.unwrap_or(0)));
                }
            }
            for sub in strings(c.get("expect_stderr")) {
                if !stderr.contains(&sub) {
                    fails.push(format!("stderr missing {sub:?} (got {:?})", stderr.trim()));
                }
            }
            for sub in strings(c.get("expect_stdout"))
                .into_iter()
                .filter(|_| !gr1_here)
            {
                if !stdout.contains(&sub) {
                    fails.push(format!("stdout missing {sub:?} (got {:?})", stdout.trim()));
                }
            }
            // The PAIR: what `--which` reports must be what a plain run ran. Asserted on every row that answers (a
            // refusal has nothing to pair, an explicit-name row never resolves), not only rows that pin `--which`.
            let paired = expect_tool != "refuse" && c.get("argv0").is_none();
            let which_out = if paired || c.get("expect_which").is_some() {
                Some(run(&["--which"]))
            } else {
                None
            };
            if paired {
                let w = which_out.as_ref().unwrap();
                let reported = String::from_utf8_lossy(&w.stdout)
                    .split_whitespace()
                    .next()
                    .unwrap_or("")
                    .to_string();
                if reported != expect_tool {
                    fails.push(format!(
                        "--which reports {reported:?} but a plain run answered {expect_tool:?}"
                    ));
                }
            }
            if let Some(want) = c.get("expect_which").and_then(|v| v.as_str()) {
                let w = which_out.as_ref().unwrap();
                let got = which_norm(&String::from_utf8_lossy(&w.stdout), &real_root);
                // In the gr binary a gr1 answer is this executable, so --which names it rather than a `gr1` on PATH.
                let want = if gr1_here {
                    let mut parts: Vec<&str> = want.split(' ').collect();
                    if let Some(last) = parts.last_mut() {
                        *last = binary.file_name().and_then(|n| n.to_str()).unwrap_or("gr");
                    }
                    parts.join(" ")
                } else {
                    want.to_string()
                };
                if got != want {
                    fails.push(format!("--which {got:?} != {want:?}"));
                }
                let w_err = String::from_utf8_lossy(&w.stderr).to_string();
                for sub in strings(c.get("expect_which_stderr")) {
                    if !w_err.contains(&sub) {
                        fails.push(format!(
                            "--which stderr missing {sub:?} (got {:?})",
                            w_err.trim()
                        ));
                    }
                }
            }
            ran += 1;
            if !fails.is_empty() {
                red.push((id.to_string(), fails));
            }
        }
        println!(
            "ran {ran} of {} rows; excluded: {}",
            cases.len(),
            if excluded.is_empty() {
                "none".to_string()
            } else {
                excluded.join(", ")
            }
        );
        if real_gr {
            println!(
                "gr1 answered in-process on {in_process} rows; {} of them carry their own args or an exec-stdout \
                 assertion, which cannot apply in-process, checked by --version instead: {}",
                in_process_named.len(),
                if in_process_named.is_empty() {
                    "none".to_string()
                } else {
                    in_process_named.join(", ")
                }
            );
        }
        assert_eq!(
            ran,
            cases.len() - excluded.len(),
            "a row was skipped silently"
        );
        let used = disk_bytes(&base_path);
        println!("scratch directory held {used} bytes");
        assert!(
            used < 10_000_000,
            "the scratch directory grew to {used} bytes: a row is copying the binary"
        );
        let detail: String = red
            .iter()
            .map(|(id, f)| format!("RED {id}\n    {}", f.join("\n    ")))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(red.is_empty(), "{detail}");
    }
}
