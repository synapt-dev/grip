//! Criterion benchmarks for comparing implementations
//!
//! Compares: git2, gitoxide (gix), git CLI, and TypeScript
//!
//! Run with: cargo bench --features gitoxide
//! Results are saved in target/criterion/ for comparison

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use gitgrip::core::manifest::{Manifest, ManifestSettings, RepoConfig};
use gitgrip::core::repo::RepoInfo;
use gitgrip::core::state::StateFile;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

/// Benchmark manifest YAML parsing
fn bench_manifest_parse(c: &mut Criterion) {
    let yaml = r#"
version: 1
manifest:
  url: git@github.com:user/manifest.git
  default_branch: main
repos:
  app:
    url: git@github.com:user/app.git
    path: app
    default_branch: main
    copyfile:
      - src: README.md
        dest: APP_README.md
    linkfile:
      - src: config.yaml
        dest: app-config.yaml
  lib:
    url: git@github.com:user/lib.git
    path: lib
    default_branch: main
  api:
    url: git@github.com:user/api.git
    path: api
    default_branch: main
settings:
  pr_prefix: "[multi-repo]"
  merge_strategy: all-or-nothing
workspace:
  env:
    NODE_ENV: development
  scripts:
    build:
      description: Build all packages
      command: npm run build
    test:
      description: Run tests
      steps:
        - name: lint
          command: npm run lint
        - name: test
          command: npm test
"#;

    c.bench_function("manifest_parse", |b| {
        b.iter(|| Manifest::parse(black_box(yaml)).unwrap())
    });
}

/// Benchmark state JSON parsing
fn bench_state_parse(c: &mut Criterion) {
    let json = r#"{
        "currentManifestPr": 42,
        "branchToPr": {
            "feat/new-feature": 42,
            "feat/another": 43,
            "fix/bug": 44
        },
        "prLinks": {
            "42": [
                {
                    "repoName": "app",
                    "owner": "user",
                    "repo": "app",
                    "number": 123,
                    "url": "https://github.com/user/app/pull/123",
                    "state": "open",
                    "approved": true,
                    "checksPass": true,
                    "mergeable": true
                },
                {
                    "repoName": "lib",
                    "owner": "user",
                    "repo": "lib",
                    "number": 456,
                    "url": "https://github.com/user/lib/pull/456",
                    "state": "open",
                    "approved": false,
                    "checksPass": true,
                    "mergeable": true
                }
            ],
            "43": [],
            "44": []
        }
    }"#;

    c.bench_function("state_parse", |b| {
        b.iter(|| StateFile::parse(black_box(json)).unwrap())
    });
}

/// Benchmark git URL parsing
fn bench_url_parse(c: &mut Criterion) {
    let config = RepoConfig {
        url: Some("git@github.com:organization/repository-name.git".to_string()),
        remote: None,
        path: "packages/repository-name".to_string(),
        revision: Some("main".to_string()),
        target: None,
        sync_remote: None,
        push_remote: None,
        copyfile: None,
        linkfile: None,
        platform: None,
        reference: false,
        groups: Vec::new(),
        agent: None,
        clone_strategy: None,
    };
    let workspace = PathBuf::from("/home/user/workspace");
    let settings = ManifestSettings::default();

    c.bench_function("url_parse_github_ssh", |b| {
        b.iter(|| {
            RepoInfo::from_config(
                "repo",
                black_box(&config),
                black_box(&workspace),
                &settings,
                None,
            )
        })
    });
}

/// Benchmark Azure DevOps URL parsing
fn bench_url_parse_azure(c: &mut Criterion) {
    let config = RepoConfig {
        url: Some("https://dev.azure.com/organization/project/_git/repository".to_string()),
        remote: None,
        path: "repository".to_string(),
        revision: Some("main".to_string()),
        target: None,
        sync_remote: None,
        push_remote: None,
        copyfile: None,
        linkfile: None,
        platform: None,
        reference: false,
        groups: Vec::new(),
        agent: None,
        clone_strategy: None,
    };
    let workspace = PathBuf::from("/home/user/workspace");
    let settings = ManifestSettings::default();

    c.bench_function("url_parse_azure_https", |b| {
        b.iter(|| {
            RepoInfo::from_config(
                "repo",
                black_box(&config),
                black_box(&workspace),
                &settings,
                None,
            )
        })
    });
}

/// Benchmark manifest validation
fn bench_manifest_validate(c: &mut Criterion) {
    let yaml = r#"
version: 1
repos:
  app:
    url: git@github.com:user/app.git
    path: app
    copyfile:
      - src: file1.txt
        dest: dest1.txt
      - src: file2.txt
        dest: dest2.txt
    linkfile:
      - src: link1
        dest: dest/link1
workspace:
  scripts:
    build:
      steps:
        - name: step1
          command: echo 1
        - name: step2
          command: echo 2
        - name: step3
          command: echo 3
"#;

    let manifest: Manifest = serde_yaml::from_str(yaml).unwrap();

    c.bench_function("manifest_validate", |b| {
        b.iter(|| black_box(&manifest).validate().unwrap())
    });
}

/// Setup a test repo and return temp dir path
fn setup_test_repo() -> tempfile::TempDir {
    use std::fs;

    let temp = tempfile::TempDir::new().unwrap();

    // Use git CLI to init (works regardless of library)
    Command::new("git")
        .args(["init"])
        .current_dir(temp.path())
        .output()
        .unwrap();

    Command::new("git")
        .args(["config", "user.name", "Bench User"])
        .current_dir(temp.path())
        .output()
        .unwrap();

    Command::new("git")
        .args(["config", "user.email", "bench@example.com"])
        .current_dir(temp.path())
        .output()
        .unwrap();

    // Create initial commit
    fs::write(temp.path().join("README.md"), "# Benchmark Repo").unwrap();

    Command::new("git")
        .args(["add", "README.md"])
        .current_dir(temp.path())
        .output()
        .unwrap();

    Command::new("git")
        .args(["commit", "-m", "Initial commit"])
        .current_dir(temp.path())
        .output()
        .unwrap();

    // Add some untracked files to make status more realistic
    for i in 0..10 {
        fs::write(
            temp.path().join(format!("file{}.txt", i)),
            format!("Content {}", i),
        )
        .unwrap();
    }

    // Create branches
    for i in 0..10 {
        Command::new("git")
            .args(["branch", &format!("branch-{}", i)])
            .current_dir(temp.path())
            .output()
            .unwrap();
    }

    temp
}

/// Compare git status implementations: git2 vs gix vs git CLI
fn bench_git_status_comparison(c: &mut Criterion) {
    let temp = setup_test_repo();

    let mut group = c.benchmark_group("git_status");

    // Benchmark git2
    {
        let repo = git2::Repository::open(temp.path()).unwrap();
        group.bench_function("git2", |b| {
            b.iter(|| {
                let statuses = repo.statuses(None).unwrap();
                black_box(statuses.len())
            })
        });
    }

    // Benchmark gix (if feature enabled)
    // Note: gix's status API is still maturing, so we benchmark repo open + head check
    #[cfg(feature = "gitoxide")]
    {
        let path = temp.path().to_path_buf();
        group.bench_function("gix", |b| {
            b.iter(|| {
                let repo = gix::open(&path).unwrap();
                // Use head_id() as a simple operation - full status API is still developing
                let head = repo.head_id();
                black_box(head.is_ok())
            })
        });
    }

    // Benchmark git CLI (porcelain)
    {
        let path = temp.path().to_path_buf();
        group.bench_function("git_cli", |b| {
            b.iter(|| {
                let output = Command::new("git")
                    .args(["status", "--porcelain"])
                    .current_dir(&path)
                    .output()
                    .unwrap();
                let lines = String::from_utf8_lossy(&output.stdout).lines().count();
                black_box(lines)
            })
        });
    }

    group.finish();
}

/// Compare branch listing implementations: git2 vs gix vs git CLI
fn bench_git_list_branches_comparison(c: &mut Criterion) {
    let temp = setup_test_repo();

    let mut group = c.benchmark_group("git_list_branches");

    // Benchmark git2
    {
        let repo = git2::Repository::open(temp.path()).unwrap();
        group.bench_function("git2", |b| {
            b.iter(|| {
                let branches: Vec<_> = repo
                    .branches(Some(git2::BranchType::Local))
                    .unwrap()
                    .collect();
                black_box(branches.len())
            })
        });
    }

    // Benchmark gix (if feature enabled)
    #[cfg(feature = "gitoxide")]
    {
        let repo = gix::open(temp.path()).unwrap();
        group.bench_function("gix", |b| {
            b.iter(|| {
                let names = repo.branch_names();
                black_box(names.len())
            })
        });
    }

    // Benchmark git CLI
    {
        let path = temp.path().to_path_buf();
        group.bench_function("git_cli", |b| {
            b.iter(|| {
                let output = Command::new("git")
                    .args(["branch", "--format=%(refname:short)"])
                    .current_dir(&path)
                    .output()
                    .unwrap();
                let count = String::from_utf8_lossy(&output.stdout).lines().count();
                black_box(count)
            })
        });
    }

    group.finish();
}

/// Compare repo open implementations: git2 vs gix
fn bench_repo_open_comparison(c: &mut Criterion) {
    let temp = setup_test_repo();

    let mut group = c.benchmark_group("repo_open");

    // Benchmark git2
    {
        let path = temp.path().to_path_buf();
        group.bench_function("git2", |b| {
            b.iter(|| {
                let repo = git2::Repository::open(black_box(&path)).unwrap();
                black_box(repo.path().to_path_buf())
            })
        });
    }

    // Benchmark gix (if feature enabled)
    #[cfg(feature = "gitoxide")]
    {
        let path = temp.path().to_path_buf();
        group.bench_function("gix", |b| {
            b.iter(|| {
                let repo = gix::open(black_box(&path)).unwrap();
                black_box(repo.path().to_path_buf())
            })
        });
    }

    group.finish();
}

/// Compare HEAD resolution: git2 vs gix vs git CLI
fn bench_get_current_branch_comparison(c: &mut Criterion) {
    let temp = setup_test_repo();

    let mut group = c.benchmark_group("get_current_branch");

    // Benchmark git2
    {
        let repo = git2::Repository::open(temp.path()).unwrap();
        group.bench_function("git2", |b| {
            b.iter(|| {
                let head = repo.head().unwrap();
                let name = head.shorthand().unwrap_or("HEAD");
                black_box(name.to_string())
            })
        });
    }

    // Benchmark gix (if feature enabled)
    #[cfg(feature = "gitoxide")]
    {
        let repo = gix::open(temp.path()).unwrap();
        group.bench_function("gix", |b| {
            b.iter(|| {
                let head = repo.head_name().unwrap();
                black_box(head.map(|n| n.shorten().to_string()))
            })
        });
    }

    // Benchmark git CLI
    {
        let path = temp.path().to_path_buf();
        group.bench_function("git_cli", |b| {
            b.iter(|| {
                let output = Command::new("git")
                    .args(["rev-parse", "--abbrev-ref", "HEAD"])
                    .current_dir(&path)
                    .output()
                    .unwrap();
                let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
                black_box(branch)
            })
        });
    }

    group.finish();
}

/// Benchmark file hashing (useful for change detection)
fn bench_file_hash(c: &mut Criterion) {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    let content = "This is some test content for hashing\n".repeat(100);

    c.bench_function("file_hash_content", |b| {
        b.iter(|| {
            let mut hasher = DefaultHasher::new();
            black_box(&content).hash(&mut hasher);
            hasher.finish()
        })
    });
}

/// Benchmark path operations (common in file linking)
fn bench_path_operations(c: &mut Criterion) {
    let workspace = PathBuf::from("/home/user/workspace");
    let repo_path = "packages/my-awesome-repo";

    c.bench_function("path_join", |b| {
        b.iter(|| {
            let full = workspace.join(black_box(repo_path));
            black_box(full)
        })
    });

    let full_path = workspace.join(repo_path);
    c.bench_function("path_canonicalize_relative", |b| {
        b.iter(|| {
            let path = black_box(&full_path);
            path.components().collect::<Vec<_>>()
        })
    });
}

/// Benchmark regex URL parsing (for platform detection)
fn bench_url_regex_parse(c: &mut Criterion) {
    use regex::Regex;

    let github_regex = Regex::new(r"github\.com[:/]([^/]+)/([^/\.]+)").unwrap();
    let gitlab_regex = Regex::new(r"gitlab\.com[:/](.+)/([^/\.]+)").unwrap();

    let url = "git@github.com:organization/repository-name.git";

    c.bench_function("url_regex_github", |b| {
        b.iter(|| github_regex.captures(black_box(url)))
    });

    let gitlab_url = "git@gitlab.com:group/subgroup/repo.git";
    c.bench_function("url_regex_gitlab", |b| {
        b.iter(|| gitlab_regex.captures(black_box(gitlab_url)))
    });
}

/// Setup multiple test repos to simulate a gitgrip workspace
fn setup_multi_repo_workspace() -> (tempfile::TempDir, Vec<PathBuf>) {
    let temp = tempfile::TempDir::new().unwrap();
    let workspace = temp.path();
    let mut repo_paths = Vec::new();

    // Create 5 repos to simulate a typical workspace
    for name in &["frontend", "backend", "shared-lib", "api", "docs"] {
        let repo_path = workspace.join(name);
        fs::create_dir_all(&repo_path).unwrap();

        Command::new("git")
            .args(["init"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        Command::new("git")
            .args(["config", "user.name", "Bench User"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        Command::new("git")
            .args(["config", "user.email", "bench@example.com"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        // Create initial commit
        fs::write(repo_path.join("README.md"), format!("# {}", name)).unwrap();

        Command::new("git")
            .args(["add", "README.md"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        Command::new("git")
            .args(["commit", "-m", "Initial commit"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        // Add some files
        for i in 0..3 {
            fs::write(
                repo_path.join(format!("file{}.txt", i)),
                format!("Content {}", i),
            )
            .unwrap();
        }

        repo_paths.push(repo_path);
    }

    (temp, repo_paths)
}

/// Benchmark forall-like command: running a command across multiple repos
fn bench_forall_command(c: &mut Criterion) {
    let (temp, repo_paths) = setup_multi_repo_workspace();

    let mut group = c.benchmark_group("forall");

    // Benchmark sequential execution (like forall without -p)
    {
        let paths = repo_paths.clone();
        group.bench_function("sequential_echo", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let output = Command::new("sh")
                        .arg("-c")
                        .arg("echo $REPO_NAME")
                        .current_dir(path)
                        .env("REPO_NAME", path.file_name().unwrap().to_str().unwrap())
                        .output()
                        .unwrap();
                    results.push(output.status.success());
                }
                black_box(results)
            })
        });
    }

    // Benchmark parallel execution (like forall -p)
    {
        use std::sync::{Arc, Mutex};
        use std::thread;

        let paths = repo_paths.clone();
        group.bench_function("parallel_echo", |b| {
            b.iter(|| {
                let results = Arc::new(Mutex::new(Vec::new()));
                let mut handles = vec![];

                for path in paths.clone() {
                    let results = Arc::clone(&results);
                    let handle = thread::spawn(move || {
                        let output = Command::new("sh")
                            .arg("-c")
                            .arg("echo $REPO_NAME")
                            .current_dir(&path)
                            .env("REPO_NAME", path.file_name().unwrap().to_str().unwrap())
                            .output()
                            .unwrap();
                        results.lock().unwrap().push(output.status.success());
                    });
                    handles.push(handle);
                }

                for handle in handles {
                    handle.join().unwrap();
                }

                black_box(Arc::try_unwrap(results).unwrap().into_inner().unwrap())
            })
        });
    }

    // Benchmark sequential git status across repos (git2) - simulates forall interception
    {
        let paths = repo_paths.clone();
        group.bench_function("sequential_git_status_git2", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = git2::Repository::open(path).unwrap();
                    let statuses = repo.statuses(None).unwrap();
                    results.push(statuses.len());
                }
                black_box(results)
            })
        });
    }

    // Benchmark forall interception: git status --porcelain (full output formatting)
    {
        let paths = repo_paths.clone();
        group.bench_function("intercepted_git_status_porcelain", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = git2::Repository::open(path).unwrap();
                    let statuses = repo.statuses(None).unwrap();

                    // Format as porcelain (what forall interception does)
                    let mut output = String::new();
                    for entry in statuses.iter() {
                        let status = entry.status();
                        let filepath = entry.path().unwrap_or("?");

                        let idx = if status.is_index_new() {
                            'A'
                        } else if status.is_index_modified() {
                            'M'
                        } else if status.is_index_deleted() {
                            'D'
                        } else {
                            ' '
                        };

                        let wt = if status.is_wt_new() {
                            '?'
                        } else if status.is_wt_modified() {
                            'M'
                        } else if status.is_wt_deleted() {
                            'D'
                        } else {
                            ' '
                        };

                        output.push_str(&format!("{}{} {}\n", idx, wt, filepath));
                    }
                    results.push(output);
                }
                black_box(results)
            })
        });
    }

    // Benchmark sequential git status across repos (gix)
    #[cfg(feature = "gitoxide")]
    {
        let paths = repo_paths.clone();
        group.bench_function("sequential_git_status_gix", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = gix::open(path).unwrap();
                    // gix status API is still maturing, so we check head_id as proxy
                    let head_ok = repo.head_id().is_ok();
                    results.push(head_ok);
                }
                black_box(results)
            })
        });
    }

    // Benchmark sequential git status via CLI (what TypeScript does)
    {
        let paths = repo_paths.clone();
        group.bench_function("sequential_git_status_cli", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let output = Command::new("git")
                        .args(["status", "--porcelain"])
                        .current_dir(path)
                        .output()
                        .unwrap();
                    results.push(String::from_utf8_lossy(&output.stdout).lines().count());
                }
                black_box(results)
            })
        });
    }

    drop(temp); // Clean up
    group.finish();
}

/// Benchmark multi-repo status gathering (gr status)
fn bench_multi_repo_status(c: &mut Criterion) {
    let (temp, repo_paths) = setup_multi_repo_workspace();

    let mut group = c.benchmark_group("multi_repo_status");

    // Full status check using git2 (branch + status + ahead/behind)
    {
        let paths = repo_paths.clone();
        group.bench_function("git2_full_status", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = git2::Repository::open(path).unwrap();

                    // Get current branch
                    let head = repo.head().unwrap();
                    let branch = head.shorthand().unwrap_or("HEAD").to_string();

                    // Get status
                    let statuses = repo.statuses(None).unwrap();
                    let has_changes = !statuses.is_empty();

                    results.push((branch, has_changes));
                }
                black_box(results)
            })
        });
    }

    // Full status check using git CLI (like TypeScript)
    {
        let paths = repo_paths.clone();
        group.bench_function("git_cli_full_status", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    // Get current branch
                    let branch_output = Command::new("git")
                        .args(["rev-parse", "--abbrev-ref", "HEAD"])
                        .current_dir(path)
                        .output()
                        .unwrap();
                    let branch = String::from_utf8_lossy(&branch_output.stdout)
                        .trim()
                        .to_string();

                    // Get status
                    let status_output = Command::new("git")
                        .args(["status", "--porcelain"])
                        .current_dir(path)
                        .output()
                        .unwrap();
                    let has_changes = !status_output.stdout.is_empty();

                    results.push((branch, has_changes));
                }
                black_box(results)
            })
        });
    }

    #[cfg(feature = "gitoxide")]
    {
        let paths = repo_paths.clone();
        group.bench_function("gix_branch_only", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = gix::open(path).unwrap();
                    let head = repo.head_name().unwrap();
                    let branch = head
                        .map(|n| n.shorten().to_string())
                        .unwrap_or_else(|| "HEAD".to_string());
                    results.push(branch);
                }
                black_box(results)
            })
        });
    }

    // Full status check using gix (branch + head_id as status proxy)
    #[cfg(feature = "gitoxide")]
    {
        let paths = repo_paths.clone();
        group.bench_function("gix_full_status", |b| {
            b.iter(|| {
                let mut results = Vec::new();
                for path in &paths {
                    let repo = gix::open(path).unwrap();

                    // Get current branch
                    let head = repo.head_name().unwrap();
                    let branch = head
                        .map(|n| n.shorten().to_string())
                        .unwrap_or_else(|| "HEAD".to_string());

                    // gix status API is still maturing - use head_id check as proxy
                    // In real usage, would use repo.status() when stable
                    let has_head = repo.head_id().is_ok();

                    results.push((branch, has_head));
                }
                black_box(results)
            })
        });
    }

    drop(temp);
    group.finish();
}

/// Benchmark manifest loading and repo resolution (gr sync prep phase)
fn bench_manifest_and_repos(c: &mut Criterion) {
    // Realistic manifest with multiple repos
    let manifest_yaml = r#"
version: 1
manifest:
  url: git@github.com:org/manifest.git
  default_branch: main
repos:
  frontend:
    url: git@github.com:org/frontend.git
    path: frontend
    default_branch: main
    copyfile:
      - src: .env.example
        dest: .env
  backend:
    url: git@github.com:org/backend.git
    path: backend
    default_branch: main
    linkfile:
      - src: shared/types.ts
        dest: frontend/src/types.ts
  shared-lib:
    url: git@github.com:org/shared-lib.git
    path: shared-lib
    default_branch: main
  api:
    url: git@github.com:org/api.git
    path: api
    default_branch: develop
  docs:
    url: git@github.com:org/docs.git
    path: docs
    default_branch: main
settings:
  pr_prefix: "[workspace]"
  merge_strategy: all-or-nothing
workspace:
  env:
    NODE_ENV: development
    API_URL: http://localhost:3000
  scripts:
    build:
      description: Build all packages
      command: npm run build
    test:
      description: Run all tests
      steps:
        - name: lint
          command: npm run lint
        - name: unit
          command: npm test
        - name: e2e
          command: npm run e2e
"#;

    c.bench_function("manifest_parse_and_validate", |b| {
        b.iter(|| {
            let manifest = Manifest::parse(black_box(manifest_yaml)).unwrap();
            manifest.validate().unwrap();
            black_box(manifest)
        })
    });

    c.bench_function("manifest_repo_resolution", |b| {
        let manifest = Manifest::parse(manifest_yaml).unwrap();
        let workspace = PathBuf::from("/home/user/workspace");

        b.iter(|| {
            let repos: Vec<_> = manifest
                .repos
                .iter()
                .filter_map(|(name, config)| {
                    RepoInfo::from_config(
                        name,
                        config,
                        black_box(&workspace),
                        &manifest.settings,
                        manifest.remotes.as_ref(),
                    )
                })
                .collect();
            black_box(repos)
        })
    });
}

/// Benchmark telemetry overhead
///
/// To compare telemetry vs no-telemetry, run:
/// ```bash
/// # With telemetry (default)
/// cargo bench --bench benchmarks -- telemetry
///
/// # Without telemetry
/// cargo bench --bench benchmarks --no-default-features -- telemetry
/// ```
#[cfg(feature = "telemetry")]
fn bench_telemetry_overhead(c: &mut Criterion) {
    use gitgrip::telemetry::metrics::GLOBAL_METRICS;
    use std::time::Duration;

    let mut group = c.benchmark_group("telemetry_overhead");

    // Benchmark metrics recording (the hot path when telemetry is enabled)
    group.bench_function("record_git_metric", |b| {
        b.iter(|| {
            GLOBAL_METRICS.record_git(
                black_box("fetch"),
                black_box(Duration::from_millis(100)),
                black_box(true),
            );
        })
    });

    group.bench_function("record_platform_metric", |b| {
        b.iter(|| {
            GLOBAL_METRICS.record_platform(
                black_box("github"),
                black_box("create_pr"),
                black_box(Duration::from_millis(500)),
                black_box(true),
            );
        })
    });

    group.bench_function("record_cache_metric", |b| {
        b.iter(|| {
            GLOBAL_METRICS.record_cache(black_box(true));
        })
    });

    group.bench_function("metrics_snapshot", |b| {
        // Add some metrics first
        for i in 0..10 {
            GLOBAL_METRICS.record_git(
                &format!("op{}", i),
                Duration::from_millis(i as u64 * 10),
                true,
            );
        }
        b.iter(|| black_box(GLOBAL_METRICS.snapshot()))
    });

    group.finish();
}

#[cfg(not(feature = "telemetry"))]
fn bench_telemetry_overhead(c: &mut Criterion) {
    // When telemetry is disabled, we measure the baseline
    let mut group = c.benchmark_group("telemetry_overhead");

    group.bench_function("baseline_noop", |b| {
        b.iter(|| {
            // This measures the baseline when telemetry is compiled out
            black_box(42)
        })
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_manifest_parse,
    bench_state_parse,
    bench_url_parse,
    bench_url_parse_azure,
    bench_manifest_validate,
    bench_git_status_comparison,
    bench_git_list_branches_comparison,
    bench_repo_open_comparison,
    bench_get_current_branch_comparison,
    bench_file_hash,
    bench_path_operations,
    bench_url_regex_parse,
    // High-level gitgrip command benchmarks
    bench_forall_command,
    bench_multi_repo_status,
    bench_manifest_and_repos,
    // Telemetry overhead benchmarks
    bench_telemetry_overhead,
);

criterion_main!(benches);
