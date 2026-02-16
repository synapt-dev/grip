//! Init command implementation
//!
//! Initializes a new gitgrip workspace.
//! Supports initialization from:
//! - A manifest URL (default)
//! - Existing local directories (--from-dirs)

use crate::cli::output::Output;
use crate::core::gripspace::{ensure_gripspace, resolve_all_gripspaces};
use crate::core::manifest::{Manifest, PlatformType, RepoConfig};
use crate::core::manifest_paths;
use crate::git::clone_repo;
use crate::platform;
use crate::util::log_cmd;
use dialoguer::{theme::ColorfulTheme, Editor, Select};
use git2::Repository;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// A discovered repository from local directories
#[derive(Debug, Clone)]
pub struct DiscoveredRepo {
    /// Repository name (directory name by default)
    pub name: String,
    /// Path relative to workspace root
    pub path: String,
    /// Absolute path on disk
    pub absolute_path: PathBuf,
    /// Remote URL if configured
    pub url: Option<String>,
    /// Default branch (main, master, etc.)
    pub default_branch: String,
}

/// Options for the init command
pub struct InitOptions<'a> {
    pub url: Option<&'a str>,
    pub path: Option<&'a str>,
    pub from_dirs: bool,
    pub dirs: &'a [String],
    pub interactive: bool,
    pub create_manifest: bool,
    pub manifest_name: Option<&'a str>,
    pub private: bool,
    pub from_repo: bool,
}

/// Run the init command
pub async fn run_init(opts: InitOptions<'_>) -> anyhow::Result<()> {
    if opts.from_repo {
        run_init_from_repo(opts.path)
    } else if opts.from_dirs {
        run_init_from_dirs(
            opts.path,
            opts.dirs,
            opts.interactive,
            opts.create_manifest,
            opts.manifest_name,
            opts.private,
        )
        .await
    } else {
        run_init_from_url(opts.url, opts.path)
    }
}

/// Initialize from an existing .repo/ directory (git-repo coexistence)
fn run_init_from_repo(path: Option<&str>) -> anyhow::Result<()> {
    use crate::core::repo_manifest::XmlManifest;

    let workspace_root = match path {
        Some(p) => PathBuf::from(p),
        None => std::env::current_dir()?,
    };

    // Find .repo directory
    let repo_dir = workspace_root.join(".repo");
    if !repo_dir.exists() {
        anyhow::bail!(
            "No .repo/ directory found in {:?}. Run 'repo init' and 'repo sync' first.",
            workspace_root
        );
    }

    // Find manifest.xml (typically a symlink to manifests/default.xml)
    let manifest_xml = repo_dir.join("manifest.xml");
    if !manifest_xml.exists() {
        anyhow::bail!("No .repo/manifest.xml found. Ensure 'repo init' has been run.");
    }

    Output::header("Initializing gitgrip from .repo/ workspace...");
    println!();

    // Parse the XML manifest
    let xml_manifest = XmlManifest::parse_file(&manifest_xml)?;
    let result = xml_manifest.to_manifest()?;

    // Print summary
    let mut platform_parts: Vec<String> = result
        .platform_counts
        .iter()
        .map(|(p, c)| format!("{}: {}", p, c))
        .collect();
    platform_parts.sort();

    Output::info(&format!(
        "Imported {} non-Gerrit repos ({})",
        result.non_gerrit_imported,
        platform_parts.join(", ")
    ));
    if result.gerrit_skipped > 0 {
        Output::info(&format!(
            "Skipped {} Gerrit repos (managed by repo upload)",
            result.gerrit_skipped
        ));
    }

    // Write manifest.yaml inside .repo/manifests/
    let manifests_dir = repo_dir.join("manifests");
    if !manifests_dir.exists() {
        anyhow::bail!(".repo/manifests/ directory not found");
    }

    let yaml = serde_yaml::to_string(&result.manifest)?;
    let yaml_path = manifests_dir.join("manifest.yaml");
    std::fs::write(&yaml_path, &yaml)?;

    // Create .gitgrip/ for state (ci results, etc.)
    let gitgrip_dir = workspace_root.join(".gitgrip");
    std::fs::create_dir_all(&gitgrip_dir)?;
    let state_path = gitgrip_dir.join("state.json");
    if !state_path.exists() {
        std::fs::write(&state_path, "{}")?;
    }

    println!();
    Output::success(&format!("Written: {}", yaml_path.display()));
    println!();
    println!("Now use: gr pr create, gr pr status, gr pr merge");

    Ok(())
}

/// Initialize workspace from a manifest URL (original behavior)
fn run_init_from_url(url: Option<&str>, path: Option<&str>) -> anyhow::Result<()> {
    let manifest_url = match url {
        Some(u) => u.to_string(),
        None => {
            anyhow::bail!("Manifest URL required. Usage: gr init <manifest-url>");
        }
    };

    // Determine target directory
    let target_dir = match path {
        Some(p) => PathBuf::from(p),
        None => {
            // Extract repo name from URL for directory name
            let name = extract_repo_name(&manifest_url).unwrap_or_else(|| "workspace".to_string());
            std::env::current_dir()?.join(name)
        }
    };

    Output::header(&format!("Initializing workspace in {:?}", target_dir));
    println!();

    // Create workspace directory
    if target_dir.exists() {
        anyhow::bail!(
            "Directory already exists: {:?}. Use a different path or remove the existing directory.",
            target_dir
        );
    }
    std::fs::create_dir_all(&target_dir)?;

    // Create .gitgrip directory structure
    let gitgrip_dir = target_dir.join(".gitgrip");
    let manifests_dir = manifest_paths::main_space_dir(&target_dir);
    let local_space_dir = manifest_paths::local_space_dir(&target_dir);
    std::fs::create_dir_all(&manifests_dir)?;
    std::fs::create_dir_all(&local_space_dir)?;

    // Clone manifest repository
    let spinner = Output::spinner("Cloning manifest repository...");
    match clone_repo(&manifest_url, &manifests_dir, None) {
        Ok(_) => {
            spinner.finish_with_message("Manifest cloned successfully");
        }
        Err(e) => {
            spinner.finish_with_message(format!("Failed to clone manifest: {}", e));
            // Clean up on failure
            let _ = std::fs::remove_dir_all(&target_dir);
            return Err(e.into());
        }
    }

    // Verify a supported manifest filename exists in the space repo.
    let manifest_path =
        if let Some(path) = manifest_paths::resolve_manifest_file_in_dir(&manifests_dir) {
            path
        } else {
            let _ = std::fs::remove_dir_all(&target_dir);
            anyhow::bail!(
                "No workspace manifest found in repository. \
             Expected gripspace.yml (preferred) or manifest.yaml/manifest.yml at repo root."
            );
        };

    // Create state file
    let state_path = gitgrip_dir.join("state.json");
    std::fs::write(&state_path, "{}")?;

    // Clone gripspaces if manifest includes them
    let manifest_content = std::fs::read_to_string(&manifest_path)?;
    let mut manifest = Manifest::parse_raw(&manifest_content)?;

    if let Some(ref gripspaces) = manifest.gripspaces {
        if !gripspaces.is_empty() {
            let spaces_dir = manifest_paths::spaces_dir(&target_dir);
            let spinner = Output::spinner(&format!("Cloning {} gripspace(s)...", gripspaces.len()));

            for gs_config in gripspaces {
                if let Err(e) = ensure_gripspace(&spaces_dir, gs_config) {
                    Output::warning(&format!(
                        "Gripspace '{}' clone failed: {}",
                        gs_config.url, e
                    ));
                    // Continue with remaining gripspaces
                    continue;
                }
            }

            spinner.finish_with_message("Gripspaces cloned");

            // Resolve gripspace includes
            if let Err(e) = resolve_all_gripspaces(&mut manifest, &spaces_dir) {
                Output::warning(&format!("Gripspace resolution failed: {}", e));
            }
        }
    }

    // Validate the (possibly merged) manifest
    if let Err(e) = manifest.validate() {
        Output::warning(&format!("Manifest validation: {}", e));
    }

    println!();
    Output::success("Workspace initialized successfully!");
    println!();
    println!("Next steps:");
    println!("  cd {:?}", target_dir);
    println!("  gr sync    # Clone all repositories");

    Ok(())
}

/// Initialize workspace from existing local directories
async fn run_init_from_dirs(
    path: Option<&str>,
    dirs: &[String],
    interactive: bool,
    create_manifest: bool,
    manifest_name: Option<&str>,
    private: bool,
) -> anyhow::Result<()> {
    // Determine workspace root
    let workspace_root = match path {
        Some(p) => PathBuf::from(p),
        None => std::env::current_dir()?,
    };

    // Check for existing workspace
    let gitgrip_dir = workspace_root.join(".gitgrip");
    if gitgrip_dir.exists() {
        anyhow::bail!(
            "A gitgrip workspace already exists at {:?}. \
             Remove .gitgrip directory to reinitialize.",
            workspace_root
        );
    }

    Output::header(&format!("Discovering repositories in {:?}", workspace_root));
    println!();

    // Discover repos
    let specific_dirs: Option<&[String]> = if dirs.is_empty() { None } else { Some(dirs) };
    let mut discovered = discover_repos(&workspace_root, specific_dirs)?;

    if discovered.is_empty() {
        anyhow::bail!(
            "No git repositories found. Make sure directories contain .git folders.\n\
             Tip: Use --dirs to specify directories explicitly."
        );
    }

    // Ensure unique names
    ensure_unique_names(&mut discovered);

    // Display discovered repos
    println!("Found {} repositories:", discovered.len());
    println!();
    for repo in &discovered {
        let url_display = repo.url.as_deref().unwrap_or("(no remote)");
        Output::list_item(&format!("{} → {} ({})", repo.name, repo.path, url_display));
    }
    println!();

    // Interactive mode
    let manifest = if interactive {
        match run_interactive_init(&workspace_root, &mut discovered)? {
            Some(m) => m,
            None => {
                Output::info("Initialization cancelled.");
                return Ok(());
            }
        }
    } else {
        generate_manifest(&discovered)
    };

    // Create .gitgrip directory structure
    let manifests_dir = manifest_paths::main_space_dir(&workspace_root);
    let local_space_dir = manifest_paths::local_space_dir(&workspace_root);
    std::fs::create_dir_all(&manifests_dir)?;
    std::fs::create_dir_all(&local_space_dir)?;

    // Write manifest
    let manifest_path = manifests_dir.join(manifest_paths::PRIMARY_FILE_NAME);
    let yaml_content = manifest_to_yaml(&manifest)?;
    std::fs::write(&manifest_path, &yaml_content)?;

    // Compatibility mirror for legacy tooling/scripts.
    let legacy_manifest_path = manifest_paths::legacy_manifest_dir(&workspace_root)
        .join(manifest_paths::LEGACY_FILE_NAMES[0]);
    if let Some(parent) = legacy_manifest_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&legacy_manifest_path, &yaml_content)?;

    // Create state file
    let state_path = gitgrip_dir.join("state.json");
    std::fs::write(&state_path, "{}")?;

    // Initialize manifest as git repo
    init_manifest_repo(&manifests_dir)?;

    // Handle manifest repo creation on detected platform
    let mut manifest_remote_url = None;
    if create_manifest {
        if let Some(detected) = detect_common_platform(&discovered) {
            let repo_name = manifest_name.unwrap_or("workspace-manifest");

            println!();
            Output::info(&format!(
                "Detected platform: {} (owner: {}, confidence: {:.0}%)",
                detected.platform,
                detected.owner,
                detected.confidence * 100.0
            ));

            let suggested_url = suggest_manifest_url(detected.platform, &detected.owner, repo_name);
            Output::info(&format!("Creating manifest repo: {}", suggested_url));

            // Create the repository
            let adapter = platform::get_platform_adapter(detected.platform, None);
            match adapter
                .create_repository(
                    &detected.owner,
                    repo_name,
                    Some("Workspace manifest repository for gitgrip"),
                    private,
                )
                .await
            {
                Ok(clone_url) => {
                    Output::success(&format!("Created repository: {}", clone_url));

                    // Add remote to manifest repo
                    let mut cmd = Command::new("git");
                    cmd.args(["remote", "add", "origin", &clone_url])
                        .current_dir(&manifests_dir);
                    log_cmd(&cmd);
                    let output = cmd.output()?;

                    if output.status.success() {
                        Output::success("Added remote 'origin' to manifest repo");
                        manifest_remote_url = Some(clone_url);

                        // Push initial commit
                        let mut cmd = Command::new("git");
                        cmd.args(["push", "-u", "origin", "main"])
                            .current_dir(&manifests_dir);
                        log_cmd(&cmd);
                        let push_output = cmd.output()?;

                        if push_output.status.success() {
                            Output::success("Pushed initial commit to remote");
                        } else {
                            // Try with master branch
                            let mut cmd = Command::new("git");
                            cmd.args(["push", "-u", "origin", "master"])
                                .current_dir(&manifests_dir);
                            log_cmd(&cmd);
                            let push_output = cmd.output()?;

                            if push_output.status.success() {
                                Output::success("Pushed initial commit to remote");
                            } else {
                                let stderr = String::from_utf8_lossy(&push_output.stderr);
                                Output::warning(&format!(
                                    "Could not push: {}. You may need to push manually.",
                                    stderr.trim()
                                ));
                            }
                        }
                    } else {
                        let stderr = String::from_utf8_lossy(&output.stderr);
                        Output::warning(&format!(
                            "Could not add remote: {}. You may need to add it manually.",
                            stderr.trim()
                        ));
                    }
                }
                Err(e) => {
                    Output::warning(&format!(
                        "Could not create repository on {}: {}",
                        detected.platform, e
                    ));
                    Output::info("You can create the repository manually and add it as a remote.");
                }
            }
        } else {
            Output::warning("Could not detect platform from repositories. No remote URLs found.");
            Output::info("You can create the manifest repository manually and add it as a remote.");
        }
    }

    println!();
    Output::success("Workspace initialized successfully!");
    println!();
    println!("Manifest created at: {}", manifest_path.display());
    println!();

    if let Some(url) = manifest_remote_url {
        println!("Manifest remote: {}", url);
        println!();
        println!("Next steps:");
        println!("  1. Review the manifest: cat .gitgrip/spaces/main/gripspace.yml");
        println!("     (legacy mirror at .gitgrip/manifests/manifest.yaml for compatibility)");
        println!("  2. Run 'gr status' to verify your workspace");
    } else {
        println!("Next steps:");
        println!("  1. Review the manifest: cat .gitgrip/spaces/main/gripspace.yml");
        println!("     (legacy mirror at .gitgrip/manifests/manifest.yaml for compatibility)");
        println!("  2. Add a remote to the manifest repo:");
        println!("     cd .gitgrip/spaces/main && git remote add origin <your-manifest-url>");
        println!("  3. Run 'gr status' to verify your workspace");
    }

    Ok(())
}

/// Discover git repositories in the given base directory
fn discover_repos(
    base_dir: &Path,
    specific_dirs: Option<&[String]>,
) -> anyhow::Result<Vec<DiscoveredRepo>> {
    let mut repos = Vec::new();

    let dirs_to_scan: Vec<PathBuf> = match specific_dirs {
        Some(dirs) => dirs
            .iter()
            .map(|d| {
                let p = PathBuf::from(d);
                if p.is_absolute() {
                    p
                } else {
                    base_dir.join(d)
                }
            })
            .collect(),
        None => {
            // Scan immediate children of base_dir
            std::fs::read_dir(base_dir)?
                .filter_map(|entry| entry.ok())
                .map(|entry| entry.path())
                .filter(|p| p.is_dir())
                .filter(|p| {
                    // Skip hidden directories
                    p.file_name()
                        .and_then(|n| n.to_str())
                        .map(|n| !n.starts_with('.'))
                        .unwrap_or(false)
                })
                .collect()
        }
    };

    for dir in dirs_to_scan {
        if let Some(repo) = try_discover_repo(base_dir, &dir)? {
            repos.push(repo);
        }
    }

    // Sort by name for consistent ordering
    repos.sort_by(|a, b| a.name.cmp(&b.name));

    Ok(repos)
}

/// Try to discover a repository in the given directory
fn try_discover_repo(workspace_root: &Path, dir: &Path) -> anyhow::Result<Option<DiscoveredRepo>> {
    // Check if it's a git repository
    let git_dir = dir.join(".git");
    if !git_dir.exists() {
        return Ok(None);
    }

    // Open the repository
    let repo = match Repository::open(dir) {
        Ok(r) => r,
        Err(_) => return Ok(None),
    };

    // Get directory name for repo name
    let name = dir
        .file_name()
        .and_then(|n| n.to_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "repo".to_string());

    // Get relative path from workspace root
    let path = dir
        .strip_prefix(workspace_root)
        .map(|p| format!("./{}", p.display()))
        .unwrap_or_else(|_| dir.display().to_string());

    // Get remote URL (prefer origin)
    let url = get_remote_url(&repo);

    // Detect default branch
    let default_branch = detect_default_branch(&repo).unwrap_or_else(|_| "main".to_string());

    Ok(Some(DiscoveredRepo {
        name,
        path,
        absolute_path: dir.to_path_buf(),
        url,
        default_branch,
    }))
}

/// Get the remote URL from a repository (preferring origin)
fn get_remote_url(repo: &Repository) -> Option<String> {
    // Try origin first
    if let Ok(remote) = repo.find_remote("origin") {
        if let Some(url) = remote.url() {
            return Some(url.to_string());
        }
    }

    // Try any remote
    if let Ok(remotes) = repo.remotes() {
        for remote_name in remotes.iter().flatten() {
            if let Ok(remote) = repo.find_remote(remote_name) {
                if let Some(url) = remote.url() {
                    return Some(url.to_string());
                }
            }
        }
    }

    None
}

/// Detect the default branch of a repository by checking the remote first
fn detect_default_branch(repo: &Repository) -> anyhow::Result<String> {
    // 1. Try origin/HEAD symbolic ref (set by git clone or git remote set-head)
    if let Ok(reference) = repo.find_reference("refs/remotes/origin/HEAD") {
        if let Ok(resolved) = reference.resolve() {
            if let Some(name) = resolved.shorthand() {
                // name is "origin/main" — strip the remote prefix
                if let Some(branch) = name.strip_prefix("origin/") {
                    return Ok(branch.to_string());
                }
            }
        }
    }

    // 2. Try common remote tracking branches
    for branch_name in &["main", "master"] {
        if repo
            .find_branch(&format!("origin/{}", branch_name), git2::BranchType::Remote)
            .is_ok()
        {
            return Ok(branch_name.to_string());
        }
    }

    // 3. Fall back to common local branch names
    for branch_name in &["main", "master", "develop"] {
        if repo
            .find_branch(branch_name, git2::BranchType::Local)
            .is_ok()
        {
            return Ok(branch_name.to_string());
        }
    }

    // 4. Default to main
    Ok("main".to_string())
}

/// Ensure all repository names are unique by adding suffixes
fn ensure_unique_names(repos: &mut [DiscoveredRepo]) {
    let mut name_counts: HashMap<String, usize> = HashMap::new();

    // First pass: count occurrences
    for repo in repos.iter() {
        *name_counts.entry(repo.name.clone()).or_insert(0) += 1;
    }

    // Second pass: rename duplicates
    let mut name_indices: HashMap<String, usize> = HashMap::new();
    for repo in repos.iter_mut() {
        if name_counts[&repo.name] > 1 {
            let idx = name_indices.entry(repo.name.clone()).or_insert(1);
            if *idx > 1 {
                repo.name = format!("{}-{}", repo.name, idx);
            }
            *idx += 1;
        }
    }
}

/// Generate a manifest from discovered repositories
fn generate_manifest(repos: &[DiscoveredRepo]) -> Manifest {
    let mut repo_configs = HashMap::new();

    for repo in repos {
        let url = repo
            .url
            .clone()
            .unwrap_or_else(|| format!("git@github.com:OWNER/{}.git", repo.name));

        repo_configs.insert(
            repo.name.clone(),
            RepoConfig {
                url,
                path: repo.path.clone(),
                default_branch: repo.default_branch.clone(),
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: Vec::new(),
                agent: None,
            },
        );
    }

    Manifest {
        version: 1,
        gripspaces: None,
        manifest: None,
        repos: repo_configs,
        settings: Default::default(),
        workspace: None,
    }
}

/// Convert a manifest to YAML string
fn manifest_to_yaml(manifest: &Manifest) -> anyhow::Result<String> {
    let yaml = serde_yaml::to_string(manifest)?;
    Ok(yaml)
}

/// Run interactive initialization
fn run_interactive_init(
    _workspace_root: &Path,
    discovered: &mut Vec<DiscoveredRepo>,
) -> anyhow::Result<Option<Manifest>> {
    let theme = ColorfulTheme::default();

    loop {
        // Show options
        let options = vec![
            "Proceed with these repositories",
            "Edit repository list",
            "Cancel",
        ];

        let selection = Select::with_theme(&theme)
            .with_prompt("What would you like to do?")
            .items(&options)
            .default(0)
            .interact()?;

        match selection {
            0 => {
                // Proceed - generate and show YAML preview
                let manifest = generate_manifest(discovered);
                let yaml = manifest_to_yaml(&manifest)?;

                println!();
                println!("Generated gripspace.yml:");
                println!("─────────────────────────────────────────");
                println!("{}", yaml);
                println!("─────────────────────────────────────────");
                println!();

                let edit_options = vec!["Use this manifest", "Edit in editor", "Go back"];

                let edit_selection = Select::with_theme(&theme)
                    .with_prompt("Review the manifest")
                    .items(&edit_options)
                    .default(0)
                    .interact()?;

                match edit_selection {
                    0 => return Ok(Some(manifest)),
                    1 => {
                        // Edit in external editor
                        if let Some(edited_yaml) = Editor::new().extension(".yaml").edit(&yaml)? {
                            // Parse and validate the edited YAML
                            match Manifest::parse(&edited_yaml) {
                                Ok(edited_manifest) => {
                                    println!();
                                    Output::success("Manifest validated successfully.");
                                    return Ok(Some(edited_manifest));
                                }
                                Err(e) => {
                                    Output::error(&format!("Invalid YAML: {}", e));
                                    println!("Please fix the errors and try again.");
                                    continue;
                                }
                            }
                        } else {
                            Output::info("No changes made.");
                            continue;
                        }
                    }
                    2 => continue,
                    _ => unreachable!(),
                }
            }
            1 => {
                // Edit repository list
                run_edit_repo_list(discovered)?;
                if discovered.is_empty() {
                    Output::warning("No repositories selected. Add at least one to continue.");
                    continue;
                }
                // Show updated list
                println!();
                println!("Selected repositories:");
                for repo in discovered.iter() {
                    Output::list_item(&format!("{} → {}", repo.name, repo.path));
                }
                println!();
            }
            2 => return Ok(None),
            _ => unreachable!(),
        }
    }
}

/// Interactive editing of the repository list
fn run_edit_repo_list(repos: &mut Vec<DiscoveredRepo>) -> anyhow::Result<()> {
    let theme = ColorfulTheme::default();

    loop {
        let mut options: Vec<String> = repos
            .iter()
            .map(|r| format!("[✓] {} ({})", r.name, r.path))
            .collect();
        options.push("Done editing".to_string());

        let selection = Select::with_theme(&theme)
            .with_prompt("Toggle repositories (select to remove)")
            .items(&options)
            .default(options.len() - 1)
            .interact()?;

        if selection == repos.len() {
            // Done editing
            break;
        }

        // Remove the selected repo
        let removed = repos.remove(selection);
        Output::info(&format!("Removed: {}", removed.name));

        if repos.is_empty() {
            Output::warning("All repositories removed.");
            break;
        }
    }

    Ok(())
}

/// Initialize the manifest directory as a git repository
fn init_manifest_repo(manifests_dir: &Path) -> anyhow::Result<()> {
    // Initialize git repo
    let mut cmd = Command::new("git");
    cmd.args(["init"]).current_dir(manifests_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("Failed to initialize manifest git repo: {}", stderr);
    }

    let manifest_file = manifest_paths::resolve_manifest_file_in_dir(manifests_dir)
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().to_string()))
        .unwrap_or_else(|| manifest_paths::PRIMARY_FILE_NAME.to_string());

    // Stage manifest file
    let mut cmd = Command::new("git");
    cmd.args(["add", &manifest_file]).current_dir(manifests_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("Failed to stage {}: {}", manifest_file, stderr);
    }

    // Create initial commit
    let mut cmd = Command::new("git");
    cmd.args([
        "commit",
        "-m",
        "Initial manifest\n\nGenerated by gr init --from-dirs",
    ])
    .current_dir(manifests_dir);
    log_cmd(&cmd);
    let output = cmd.output()?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        // Don't fail if commit fails (e.g., no git user configured)
        Output::warning(&format!(
            "Could not create initial commit: {}. You may need to commit manually.",
            stderr.trim()
        ));
    }

    Ok(())
}

/// Extract repository name from URL
fn extract_repo_name(url: &str) -> Option<String> {
    // Handle SSH URLs: git@github.com:owner/repo.git
    if url.starts_with("git@") {
        let parts: Vec<&str> = url.split('/').collect();
        if let Some(last) = parts.last() {
            return Some(last.trim_end_matches(".git").to_string());
        }
    }

    // Handle HTTPS URLs: https://github.com/owner/repo.git
    if url.starts_with("https://") || url.starts_with("http://") {
        let parts: Vec<&str> = url.split('/').collect();
        if let Some(last) = parts.last() {
            return Some(last.trim_end_matches(".git").to_string());
        }
    }

    None
}

/// Result of platform detection from discovered repos
#[derive(Debug, Clone)]
pub struct DetectedPlatform {
    /// The detected platform type
    pub platform: PlatformType,
    /// The owner/organization on that platform
    pub owner: String,
    /// Confidence level (number of repos with this platform / total repos with remotes)
    pub confidence: f32,
}

/// Analyze discovered repos and detect their common platform
///
/// Returns the most common platform among repos with remotes, along with
/// the detected owner/organization. Returns None if no repos have remotes
/// or if there's no clear majority platform.
pub fn detect_common_platform(repos: &[DiscoveredRepo]) -> Option<DetectedPlatform> {
    // Filter to repos with URLs
    let repos_with_urls: Vec<_> = repos.iter().filter_map(|r| r.url.as_ref()).collect();

    if repos_with_urls.is_empty() {
        return None;
    }

    // Count platforms and collect owners
    let mut platform_counts: HashMap<PlatformType, Vec<String>> = HashMap::new();

    for url in &repos_with_urls {
        let detected_platform = platform::detect_platform(url);
        let adapter = platform::get_platform_adapter(detected_platform, None);

        if let Some(info) = adapter.parse_repo_url(url) {
            platform_counts
                .entry(detected_platform)
                .or_default()
                .push(info.owner);
        } else {
            // URL matches platform but couldn't be parsed - still count it
            platform_counts.entry(detected_platform).or_default();
        }
    }

    // Find the platform with the most repos
    let (platform, owners) = platform_counts
        .into_iter()
        .max_by_key(|(_, owners)| owners.len())?;

    // Find the most common owner for this platform
    let mut owner_counts: HashMap<String, usize> = HashMap::new();
    for owner in &owners {
        *owner_counts.entry(owner.clone()).or_insert(0) += 1;
    }

    let (owner, _) = owner_counts.into_iter().max_by_key(|(_, count)| *count)?;

    let confidence = owners.len() as f32 / repos_with_urls.len() as f32;

    Some(DetectedPlatform {
        platform,
        owner,
        confidence,
    })
}

/// Generate a suggested manifest repo URL based on the detected platform
pub fn suggest_manifest_url(platform: PlatformType, owner: &str, name: &str) -> String {
    match platform {
        PlatformType::GitHub => format!("git@github.com:{}/{}.git", owner, name),
        PlatformType::GitLab => format!("git@gitlab.com:{}/{}.git", owner, name),
        PlatformType::AzureDevOps => {
            // Azure DevOps owner format: org/project
            // SSH URL format: git@ssh.dev.azure.com:v3/org/project/repo
            format!("git@ssh.dev.azure.com:v3/{}/{}.git", owner, name)
        }
        PlatformType::Bitbucket => format!("git@bitbucket.org:{}/{}.git", owner, name),
    }
}

/// Generate an HTTPS URL for the manifest repo based on the detected platform
pub fn suggest_manifest_https_url(platform: PlatformType, owner: &str, name: &str) -> String {
    match platform {
        PlatformType::GitHub => format!("https://github.com/{}/{}.git", owner, name),
        PlatformType::GitLab => format!("https://gitlab.com/{}/{}.git", owner, name),
        PlatformType::AzureDevOps => {
            // Azure DevOps owner format: org/project
            // HTTPS URL format: https://dev.azure.com/org/project/_git/repo
            let parts: Vec<&str> = owner.split('/').collect();
            if parts.len() >= 2 {
                format!(
                    "https://dev.azure.com/{}/{}/_git/{}",
                    parts[0], parts[1], name
                )
            } else {
                // Fallback: use owner as both org and project
                format!("https://dev.azure.com/{}/{}/_git/{}", owner, owner, name)
            }
        }
        PlatformType::Bitbucket => format!("https://bitbucket.org/{}/{}.git", owner, name),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_extract_repo_name_ssh() {
        assert_eq!(
            extract_repo_name("git@github.com:user/my-workspace.git"),
            Some("my-workspace".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_https() {
        assert_eq!(
            extract_repo_name("https://github.com/user/my-workspace.git"),
            Some("my-workspace".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_no_extension() {
        assert_eq!(
            extract_repo_name("https://github.com/user/workspace"),
            Some("workspace".to_string())
        );
    }

    #[test]
    fn test_ensure_unique_names() {
        let mut repos = vec![
            DiscoveredRepo {
                name: "app".to_string(),
                path: "./app1".to_string(),
                absolute_path: PathBuf::from("/tmp/app1"),
                url: None,
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "app".to_string(),
                path: "./app2".to_string(),
                absolute_path: PathBuf::from("/tmp/app2"),
                url: None,
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "backend".to_string(),
                path: "./backend".to_string(),
                absolute_path: PathBuf::from("/tmp/backend"),
                url: None,
                default_branch: "main".to_string(),
            },
        ];

        ensure_unique_names(&mut repos);

        // First "app" keeps its name, second gets "-2"
        assert_eq!(repos[0].name, "app");
        assert_eq!(repos[1].name, "app-2");
        assert_eq!(repos[2].name, "backend");
    }

    #[test]
    fn test_generate_manifest() {
        let repos = vec![
            DiscoveredRepo {
                name: "frontend".to_string(),
                path: "./frontend".to_string(),
                absolute_path: PathBuf::from("/tmp/frontend"),
                url: Some("git@github.com:org/frontend.git".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "backend".to_string(),
                path: "./backend".to_string(),
                absolute_path: PathBuf::from("/tmp/backend"),
                url: None,
                default_branch: "master".to_string(),
            },
        ];

        let manifest = generate_manifest(&repos);

        assert_eq!(manifest.repos.len(), 2);
        assert!(manifest.repos.contains_key("frontend"));
        assert!(manifest.repos.contains_key("backend"));
        assert_eq!(
            manifest.repos["frontend"].url,
            "git@github.com:org/frontend.git"
        );
        assert_eq!(manifest.repos["frontend"].default_branch, "main");
        // Backend should have placeholder URL
        assert!(manifest.repos["backend"].url.contains("OWNER"));
        assert_eq!(manifest.repos["backend"].default_branch, "master");
    }

    #[test]
    fn test_discover_repos_empty() {
        let temp = TempDir::new().unwrap();
        let repos = discover_repos(temp.path(), None).unwrap();
        assert!(repos.is_empty());
    }

    #[test]
    fn test_discover_repos_with_git_dir() {
        let temp = TempDir::new().unwrap();

        // Create a subdirectory with a git repo
        let repo_dir = temp.path().join("my-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        Repository::init(&repo_dir).unwrap();

        let repos = discover_repos(temp.path(), None).unwrap();
        assert_eq!(repos.len(), 1);
        assert_eq!(repos[0].name, "my-repo");
    }

    #[test]
    fn test_discover_repos_skips_hidden() {
        let temp = TempDir::new().unwrap();

        // Create a hidden directory with a git repo
        let hidden_dir = temp.path().join(".hidden-repo");
        std::fs::create_dir_all(&hidden_dir).unwrap();
        Repository::init(&hidden_dir).unwrap();

        // Create a normal directory with a git repo
        let repo_dir = temp.path().join("visible-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        Repository::init(&repo_dir).unwrap();

        let repos = discover_repos(temp.path(), None).unwrap();
        assert_eq!(repos.len(), 1);
        assert_eq!(repos[0].name, "visible-repo");
    }

    #[test]
    fn test_manifest_to_yaml() {
        let repos = vec![DiscoveredRepo {
            name: "test".to_string(),
            path: "./test".to_string(),
            absolute_path: PathBuf::from("/tmp/test"),
            url: Some("git@github.com:org/test.git".to_string()),
            default_branch: "main".to_string(),
        }];

        let manifest = generate_manifest(&repos);
        let yaml = manifest_to_yaml(&manifest).unwrap();

        assert!(yaml.contains("repos:"));
        assert!(yaml.contains("test:"));
        assert!(yaml.contains("git@github.com:org/test.git"));
    }

    #[test]
    fn test_detect_github_platform() {
        let repos = vec![
            DiscoveredRepo {
                name: "frontend".to_string(),
                path: "./frontend".to_string(),
                absolute_path: PathBuf::from("/tmp/frontend"),
                url: Some("git@github.com:myorg/frontend.git".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "backend".to_string(),
                path: "./backend".to_string(),
                absolute_path: PathBuf::from("/tmp/backend"),
                url: Some("git@github.com:myorg/backend.git".to_string()),
                default_branch: "main".to_string(),
            },
        ];

        let result = detect_common_platform(&repos);
        assert!(result.is_some());
        let detected = result.unwrap();
        assert_eq!(detected.platform, PlatformType::GitHub);
        assert_eq!(detected.owner, "myorg");
        assert_eq!(detected.confidence, 1.0);
    }

    #[test]
    fn test_detect_azure_platform() {
        let repos = vec![
            DiscoveredRepo {
                name: "app".to_string(),
                path: "./app".to_string(),
                absolute_path: PathBuf::from("/tmp/app"),
                url: Some("git@ssh.dev.azure.com:v3/myorg/myproject/app".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "lib".to_string(),
                path: "./lib".to_string(),
                absolute_path: PathBuf::from("/tmp/lib"),
                url: Some("https://dev.azure.com/myorg/myproject/_git/lib".to_string()),
                default_branch: "main".to_string(),
            },
        ];

        let result = detect_common_platform(&repos);
        assert!(result.is_some());
        let detected = result.unwrap();
        assert_eq!(detected.platform, PlatformType::AzureDevOps);
        assert_eq!(detected.owner, "myorg/myproject");
    }

    #[test]
    fn test_detect_gitlab_platform() {
        let repos = vec![
            DiscoveredRepo {
                name: "frontend".to_string(),
                path: "./frontend".to_string(),
                absolute_path: PathBuf::from("/tmp/frontend"),
                url: Some("git@gitlab.com:mygroup/frontend.git".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "backend".to_string(),
                path: "./backend".to_string(),
                absolute_path: PathBuf::from("/tmp/backend"),
                url: Some("https://gitlab.com/mygroup/backend.git".to_string()),
                default_branch: "main".to_string(),
            },
        ];

        let result = detect_common_platform(&repos);
        assert!(result.is_some());
        let detected = result.unwrap();
        assert_eq!(detected.platform, PlatformType::GitLab);
        assert_eq!(detected.owner, "mygroup");
    }

    #[test]
    fn test_detect_no_remotes() {
        let repos = vec![
            DiscoveredRepo {
                name: "local1".to_string(),
                path: "./local1".to_string(),
                absolute_path: PathBuf::from("/tmp/local1"),
                url: None,
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "local2".to_string(),
                path: "./local2".to_string(),
                absolute_path: PathBuf::from("/tmp/local2"),
                url: None,
                default_branch: "main".to_string(),
            },
        ];

        let result = detect_common_platform(&repos);
        assert!(result.is_none());
    }

    #[test]
    fn test_detect_mixed_platforms() {
        let repos = vec![
            DiscoveredRepo {
                name: "gh1".to_string(),
                path: "./gh1".to_string(),
                absolute_path: PathBuf::from("/tmp/gh1"),
                url: Some("git@github.com:org1/gh1.git".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "gh2".to_string(),
                path: "./gh2".to_string(),
                absolute_path: PathBuf::from("/tmp/gh2"),
                url: Some("git@github.com:org1/gh2.git".to_string()),
                default_branch: "main".to_string(),
            },
            DiscoveredRepo {
                name: "gl1".to_string(),
                path: "./gl1".to_string(),
                absolute_path: PathBuf::from("/tmp/gl1"),
                url: Some("git@gitlab.com:org2/gl1.git".to_string()),
                default_branch: "main".to_string(),
            },
        ];

        let result = detect_common_platform(&repos);
        assert!(result.is_some());
        let detected = result.unwrap();
        // GitHub should win (2 vs 1)
        assert_eq!(detected.platform, PlatformType::GitHub);
        assert_eq!(detected.owner, "org1");
        // Confidence should be 2/3
        assert!((detected.confidence - 0.666).abs() < 0.01);
    }

    #[test]
    fn test_suggest_manifest_url_github() {
        let url = suggest_manifest_url(PlatformType::GitHub, "myorg", "workspace-manifest");
        assert_eq!(url, "git@github.com:myorg/workspace-manifest.git");
    }

    #[test]
    fn test_suggest_manifest_url_gitlab() {
        let url = suggest_manifest_url(PlatformType::GitLab, "mygroup", "workspace-manifest");
        assert_eq!(url, "git@gitlab.com:mygroup/workspace-manifest.git");
    }

    #[test]
    fn test_suggest_manifest_url_azure() {
        let url = suggest_manifest_url(
            PlatformType::AzureDevOps,
            "myorg/myproject",
            "workspace-manifest",
        );
        assert_eq!(
            url,
            "git@ssh.dev.azure.com:v3/myorg/myproject/workspace-manifest.git"
        );
    }

    #[test]
    fn test_suggest_manifest_https_url_github() {
        let url = suggest_manifest_https_url(PlatformType::GitHub, "myorg", "workspace-manifest");
        assert_eq!(url, "https://github.com/myorg/workspace-manifest.git");
    }

    #[test]
    fn test_suggest_manifest_https_url_azure() {
        let url = suggest_manifest_https_url(
            PlatformType::AzureDevOps,
            "myorg/myproject",
            "workspace-manifest",
        );
        assert_eq!(
            url,
            "https://dev.azure.com/myorg/myproject/_git/workspace-manifest"
        );
    }

    // ── detect_default_branch tests ─────────────────────────────

    fn setup_git_repo(dir: &std::path::Path) -> Repository {
        let repo = Repository::init(dir).unwrap();
        let sig = git2::Signature::now("Test", "test@test.com").unwrap();
        let tree_id = {
            let mut index = repo.index().unwrap();
            index.write_tree().unwrap()
        };
        {
            let tree = repo.find_tree(tree_id).unwrap();
            repo.commit(Some("HEAD"), &sig, &sig, "init", &tree, &[])
                .unwrap();
        }
        repo
    }

    #[test]
    fn test_detect_default_branch_with_origin_head() {
        let tmp = TempDir::new().unwrap();
        let origin_dir = tmp.path().join("origin");
        std::fs::create_dir_all(&origin_dir).unwrap();

        // Create a bare "remote" repo with main as default branch
        let origin = Repository::init_bare(&origin_dir).unwrap();
        origin.set_head("refs/heads/main").unwrap();
        let sig = git2::Signature::now("Test", "test@test.com").unwrap();
        let tree_id = origin.treebuilder(None).unwrap().write().unwrap();
        {
            let tree = origin.find_tree(tree_id).unwrap();
            origin
                .commit(Some("refs/heads/main"), &sig, &sig, "init", &tree, &[])
                .unwrap();
        }

        // Clone it (sets origin/HEAD automatically)
        let clone_dir = tmp.path().join("clone");
        let repo = Repository::clone(origin_dir.to_str().unwrap(), &clone_dir).unwrap();

        // Create and checkout a feature branch
        let head_commit = repo.head().unwrap().peel_to_commit().unwrap();
        repo.branch("feat/something", &head_commit, false).unwrap();
        repo.set_head("refs/heads/feat/something").unwrap();

        // Should detect "main" from origin/HEAD, not "feat/something"
        let result = detect_default_branch(&repo).unwrap();
        assert_eq!(result, "main");
    }

    #[test]
    fn test_detect_default_branch_remote_tracking_main() {
        let tmp = TempDir::new().unwrap();

        // Create a local repo with a remote tracking branch but no origin/HEAD
        let repo = setup_git_repo(tmp.path());

        // Create origin/main as a remote tracking ref
        let head_commit = repo.head().unwrap().peel_to_commit().unwrap();
        repo.reference("refs/remotes/origin/main", head_commit.id(), true, "test")
            .unwrap();

        // Rename local branch to a feature branch
        let mut branch = repo
            .find_branch("master", git2::BranchType::Local)
            .or_else(|_| repo.find_branch("main", git2::BranchType::Local))
            .unwrap();
        branch.rename("feat/work", false).unwrap();

        let result = detect_default_branch(&repo).unwrap();
        assert_eq!(result, "main");
    }

    #[test]
    fn test_detect_default_branch_remote_tracking_master() {
        let tmp = TempDir::new().unwrap();
        let repo = setup_git_repo(tmp.path());

        // Create origin/master as remote tracking ref (no origin/main)
        let head_commit = repo.head().unwrap().peel_to_commit().unwrap();
        repo.reference("refs/remotes/origin/master", head_commit.id(), true, "test")
            .unwrap();

        // Rename local branch to feature branch
        let mut branch = repo
            .find_branch("master", git2::BranchType::Local)
            .or_else(|_| repo.find_branch("main", git2::BranchType::Local))
            .unwrap();
        branch.rename("feat/work", false).unwrap();

        let result = detect_default_branch(&repo).unwrap();
        assert_eq!(result, "master");
    }

    #[test]
    fn test_detect_default_branch_local_main_only() {
        let tmp = TempDir::new().unwrap();
        let repo = setup_git_repo(tmp.path());

        // Ensure there's a local "main" branch
        let head_commit = repo.head().unwrap().peel_to_commit().unwrap();
        // The initial branch could be "master" depending on git config
        if repo.find_branch("main", git2::BranchType::Local).is_err() {
            repo.branch("main", &head_commit, false).unwrap();
        }

        // Switch to a feature branch
        repo.branch("feat/test", &head_commit, false).unwrap();
        repo.set_head("refs/heads/feat/test").unwrap();

        let result = detect_default_branch(&repo).unwrap();
        // Should find local "main" or "master", not "feat/test"
        assert!(result == "main" || result == "master");
    }

    #[test]
    fn test_detect_default_branch_empty_repo() {
        let tmp = TempDir::new().unwrap();
        let _repo = Repository::init(tmp.path()).unwrap();

        // Empty repo with no commits — no branches exist
        let repo = Repository::open(tmp.path()).unwrap();
        let result = detect_default_branch(&repo).unwrap();
        assert_eq!(result, "main"); // Falls back to default
    }
}
