//! Gripspace include resolution
//!
//! Gripspaces allow composable manifest inheritance. A workspace manifest can
//! include one or more gripspace repositories, inheriting their repos, scripts,
//! env vars, hooks, and linkfiles. Local values always win on conflict.
//!
//! ## Merge ordering
//!
//! Resolution is depth-first. For hooks (post_sync, post_checkout), the ordering
//! is: deepest gripspace hooks first, then their parent's hooks, then the local
//! workspace hooks last. Maps (repos, scripts, env) use `entry().or_insert()` so
//! the first definition wins — local definitions take priority because they are
//! inserted after gripspace values, overriding by key.

use crate::core::manifest::{
    GripspaceConfig, HookCommand, Manifest, ManifestError, WorkspaceAgentConfig, WorkspaceConfig,
    WorkspaceHooks,
};
use crate::core::manifest_paths;
use crate::git::clone_repo;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;

/// Maximum depth for recursive gripspace includes
const MAX_GRIPSPACE_DEPTH: usize = 5;

/// Extract a gripspace name from its URL.
///
/// Takes the last path component without `.git` suffix.
/// e.g., `https://github.com/user/codi-gripspace.git` -> `codi-gripspace`
///
/// Note: For SSH URLs like `git@host:org/group/repo.git`, only the final
/// component `repo` is extracted. If two gripspaces share the same repo name
/// under different groups, they will collide. Use `rev` to disambiguate or
/// rename one of the repositories.
pub fn gripspace_name(url: &str) -> String {
    let url = url.trim_end_matches('/');
    let last = url.rsplit('/').next().unwrap_or(url);
    // Handle SSH URLs like git@github.com:user/repo.git
    let last = last.rsplit(':').next().unwrap_or(last);
    let last = last.rsplit('/').next().unwrap_or(last);
    last.trim_end_matches(".git").to_string()
}

fn gripspace_identity(config: &GripspaceConfig) -> String {
    let url = normalize_url(&config.url);
    match &config.rev {
        Some(rev) if !rev.is_empty() => format!("{}#{}", url, rev),
        _ => url,
    }
}

fn validate_space_name(name: &str) -> Result<(), ManifestError> {
    if name.is_empty() || name == "." || name == ".." {
        return Err(ManifestError::GripspaceError(format!(
            "Invalid gripspace name '{}': empty or reserved path segment",
            name
        )));
    }

    // Allowlist to keep names safe for filesystem path joins.
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    {
        return Err(ManifestError::GripspaceError(format!(
            "Invalid gripspace name '{}': only [a-zA-Z0-9._-] are allowed",
            name
        )));
    }

    if name.contains("..") {
        return Err(ManifestError::GripspaceError(format!(
            "Invalid gripspace name '{}': '..' is not allowed",
            name
        )));
    }

    Ok(())
}

/// Resolve the directory name for a gripspace within `.gitgrip/spaces/`.
///
/// Handles reserved name conflicts (`main`, `local`) and duplicate names by
/// auto-suffixing with `-1`, `-2`, etc. If the directory already exists and
/// has the same git remote, it is reused.
pub fn resolve_space_name(url: &str, spaces_dir: &Path) -> Result<String, ManifestError> {
    let base = gripspace_name(url);
    validate_space_name(&base)?;

    // If the base name is reserved, start from suffix -1
    let candidate = if manifest_paths::RESERVED_SPACE_NAMES.contains(&base.as_str()) {
        format!("{}-1", base)
    } else {
        base.clone()
    };

    let candidate_path = spaces_dir.join(&candidate);
    if !candidate_path.exists() {
        return Ok(candidate);
    }

    // Directory exists — reuse if it's the same remote
    if is_same_remote(&candidate_path, url) {
        return Ok(candidate);
    }

    // Warn when an unrecognized directory occupies the expected name
    if !candidate_path.join(".git").exists() {
        eprintln!(
            "Warning: '{}' exists but is not a git repository; using alternate name",
            candidate_path.display()
        );
    }

    // Auto-increment to find a free name
    for i in 2..100 {
        let suffixed = format!("{}-{}", base, i);
        let path = spaces_dir.join(&suffixed);
        if !path.exists() || is_same_remote(&path, url) {
            return Ok(suffixed);
        }
    }

    Err(ManifestError::GripspaceError(format!(
        "Could not allocate a space name for '{}' (too many collisions)",
        url
    )))
}

/// Check whether an existing directory should be reused for the given URL.
///
/// Returns `true` if:
/// - The directory is a git repository and its origin remote URL matches
///   after normalization.
fn is_same_remote(dir: &Path, url: &str) -> bool {
    if !dir.join(".git").exists() {
        return false;
    }

    let output = Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(dir)
        .output();

    match output {
        Ok(out) if out.status.success() => {
            let existing = String::from_utf8_lossy(&out.stdout).trim().to_string();
            normalize_url(&existing) == normalize_url(url)
        }
        Ok(_) => false,
        _ => false,
    }
}

/// Normalize a git URL for comparison.
///
/// For known remote URL forms, this canonicalizes to `<host>:<path>`:
/// - `https://host/org/repo.git`
/// - `ssh://git@host/org/repo.git`
/// - `git@host:org/repo.git`
fn normalize_url(url: &str) -> String {
    let trimmed = url.trim().trim_end_matches('/').trim_end_matches(".git");

    // SCP-like SSH URL: git@host:org/repo  (or bare host:path)
    if !trimmed.contains("://") {
        if let Some((user_host, path)) = trimmed.split_once(':') {
            // Extract host from "user@host" or use the whole segment if no '@'
            let host = user_host.rsplit('@').next().unwrap_or(user_host);
            if !host.is_empty() && !path.is_empty() {
                return format!(
                    "{}:{}",
                    host.to_ascii_lowercase(),
                    path.trim_start_matches('/')
                );
            }
        }
    }

    // Scheme URL: https://host/org/repo or ssh://git@host/org/repo
    if let Some((_, rest)) = trimmed.split_once("://") {
        if let Some((host_user, path)) = rest.split_once('/') {
            let host = host_user.rsplit('@').next().unwrap_or(host_user);
            if !host.is_empty() && !path.is_empty() {
                return format!(
                    "{}:{}",
                    host.to_ascii_lowercase(),
                    path.trim_start_matches('/')
                );
            }
        }
    }

    trimmed.to_string()
}

/// Ensure a gripspace is cloned locally. Returns the path to the gripspace directory.
///
/// If the gripspace is already cloned, this is a no-op.
/// The gripspace is cloned into `spaces_dir/<resolved_name>/`.
pub fn ensure_gripspace(
    spaces_dir: &Path,
    config: &GripspaceConfig,
) -> Result<PathBuf, ManifestError> {
    let dir_name = resolve_space_name(&config.url, spaces_dir)?;
    let gripspace_path = spaces_dir.join(&dir_name);

    if gripspace_path.exists() {
        // Already cloned, just checkout the right rev if specified
        if let Some(ref rev) = config.rev {
            checkout_rev(&gripspace_path, rev)?;
        }
        return Ok(gripspace_path);
    }

    // Clone the gripspace
    std::fs::create_dir_all(spaces_dir).map_err(|e| {
        ManifestError::GripspaceError(format!("Failed to create spaces dir: {}", e))
    })?;

    if let Err(e) = clone_repo(&config.url, &gripspace_path, None) {
        // Clean up partial clone to avoid confusing subsequent runs
        let _ = std::fs::remove_dir_all(&gripspace_path);
        return Err(ManifestError::GripspaceError(format!(
            "Failed to clone gripspace '{}': {}",
            config.url, e
        )));
    }

    // Checkout specific revision if specified
    if let Some(ref rev) = config.rev {
        checkout_rev(&gripspace_path, rev)?;
    }

    Ok(gripspace_path)
}

/// Update a gripspace by fetching and pulling latest.
pub fn update_gripspace(
    gripspace_path: &Path,
    config: &GripspaceConfig,
) -> Result<(), ManifestError> {
    if !gripspace_path.exists() {
        return Err(ManifestError::GripspaceError(format!(
            "Gripspace directory does not exist: {}",
            gripspace_path.display()
        )));
    }

    // Fetch from origin
    let output = Command::new("git")
        .args(["fetch", "origin"])
        .current_dir(gripspace_path)
        .output()
        .map_err(|e| ManifestError::GripspaceError(format!("Failed to fetch gripspace: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(ManifestError::GripspaceError(format!(
            "Failed to fetch gripspace: {}",
            stderr.trim()
        )));
    }

    // Checkout specific rev or pull latest
    if let Some(ref rev) = config.rev {
        checkout_rev(gripspace_path, rev)?;
    } else {
        // Pull latest on current branch
        let output = Command::new("git")
            .args(["pull", "--ff-only"])
            .current_dir(gripspace_path)
            .output()
            .map_err(|e| {
                ManifestError::GripspaceError(format!("Failed to pull gripspace: {}", e))
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            // Non-fatal: gripspace may be on a detached HEAD or have diverged
            // Warn but don't silently reset — that could discard local changes
            return Err(ManifestError::GripspaceError(format!(
                "Failed to pull gripspace (try specifying a rev): {}",
                stderr.trim()
            )));
        }
    }

    Ok(())
}

/// Checkout a specific revision (branch, tag, or SHA) in a gripspace.
fn checkout_rev(path: &Path, rev: &str) -> Result<(), ManifestError> {
    // Reject revs that look like flags or contain whitespace
    if rev.starts_with('-') || rev.chars().any(|c| c.is_whitespace()) || rev.is_empty() {
        return Err(ManifestError::GripspaceError(format!(
            "Invalid rev '{}': must not be empty, start with '-', or contain whitespace",
            rev
        )));
    }

    let output = Command::new("git")
        .args(["checkout", rev])
        .current_dir(path)
        .output()
        .map_err(|e| {
            ManifestError::GripspaceError(format!("Failed to checkout rev '{}': {}", rev, e))
        })?;

    if !output.status.success() {
        // Try as a remote branch
        let output = Command::new("git")
            .args(["checkout", "-B", rev, &format!("origin/{}", rev)])
            .current_dir(path)
            .output()
            .map_err(|e| {
                ManifestError::GripspaceError(format!("Failed to checkout rev '{}': {}", rev, e))
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(ManifestError::GripspaceError(format!(
                "Failed to checkout rev '{}': {}",
                rev,
                stderr.trim()
            )));
        }
    }

    Ok(())
}

/// Deep-merge gripspace repo config underneath local overrides.
///
/// Local scalar fields win when explicitly set. Collections (groups, linkfile, copyfile)
/// are unioned with deduplication. Optional struct fields (agent, platform) fall back to
/// the gripspace version when the local manifest doesn't define them.
fn deep_merge_repo_config(
    local: &mut crate::core::manifest::RepoConfig,
    gripspace: &crate::core::manifest::RepoConfig,
) {
    // Scalar fields: local wins if set, otherwise inherit from gripspace
    if local.url.is_none() {
        local.url.clone_from(&gripspace.url);
    }
    if local.remote.is_none() {
        local.remote.clone_from(&gripspace.remote);
    }
    if local.revision.is_none() {
        local.revision.clone_from(&gripspace.revision);
    }
    if local.target.is_none() {
        local.target.clone_from(&gripspace.target);
    }
    if local.sync_remote.is_none() {
        local.sync_remote.clone_from(&gripspace.sync_remote);
    }
    if local.push_remote.is_none() {
        local.push_remote.clone_from(&gripspace.push_remote);
    }

    // Optional struct fields: local wins if present, otherwise inherit
    if local.platform.is_none() {
        local.platform.clone_from(&gripspace.platform);
    }
    if local.agent.is_none() {
        local.agent.clone_from(&gripspace.agent);
    }

    // reference: only inherit if gripspace sets it true and local doesn't
    if !local.reference && gripspace.reference {
        local.reference = true;
    }

    // Groups: union with deduplication (gripspace groups + local groups)
    if !gripspace.groups.is_empty() {
        let existing: HashSet<String> = local.groups.iter().cloned().collect();
        for g in &gripspace.groups {
            if !existing.contains(g) {
                local.groups.push(g.clone());
            }
        }
    }

    // Linkfiles: union, deduplicated by dest (local wins on conflict)
    if let Some(gs_linkfiles) = &gripspace.linkfile {
        let local_linkfiles = local.linkfile.get_or_insert_with(Vec::new);
        let local_dests: HashSet<String> = local_linkfiles.iter().map(|l| l.dest.clone()).collect();
        for lf in gs_linkfiles {
            if !local_dests.contains(&lf.dest) {
                local_linkfiles.push(lf.clone());
            }
        }
    }

    // Copyfiles: union, deduplicated by dest (local wins on conflict)
    if let Some(gs_copyfiles) = &gripspace.copyfile {
        let local_copyfiles = local.copyfile.get_or_insert_with(Vec::new);
        let local_dests: HashSet<String> = local_copyfiles.iter().map(|c| c.dest.clone()).collect();
        for cf in gs_copyfiles {
            if !local_dests.contains(&cf.dest) {
                local_copyfiles.push(cf.clone());
            }
        }
    }
}

/// Resolve all gripspaces: clone/load their manifests, merge into the local manifest.
///
/// Processes gripspaces in order, with recursive include support.
/// Local manifest values always win on conflicts.
pub fn resolve_all_gripspaces(
    manifest: &mut Manifest,
    spaces_dir: &Path,
) -> Result<(), ManifestError> {
    let gripspaces = match manifest.gripspaces.take() {
        Some(gs) if !gs.is_empty() => gs,
        _ => return Ok(()),
    };

    let mut active_stack = HashSet::new();
    let mut resolved = HashSet::new();
    let mut merged_repos = HashMap::new();
    let mut merged_scripts = HashMap::new();
    let mut merged_env = HashMap::new();
    let mut merged_hooks_post_sync: Vec<HookCommand> = Vec::new();
    let mut merged_hooks_post_checkout: Vec<HookCommand> = Vec::new();
    let mut merged_linkfiles = Vec::new();
    let mut merged_copyfiles = Vec::new();
    let mut merged_agent: Option<WorkspaceAgentConfig> = None;

    // Process each gripspace
    for gs_config in &gripspaces {
        resolve_gripspace_recursive(
            gs_config,
            spaces_dir,
            &mut active_stack,
            &mut resolved,
            0,
            &mut merged_repos,
            &mut merged_scripts,
            &mut merged_env,
            &mut merged_hooks_post_sync,
            &mut merged_hooks_post_checkout,
            &mut merged_linkfiles,
            &mut merged_copyfiles,
            &mut merged_agent,
        )?;
    }

    // Now merge gripspace values into the manifest, with local values winning

    // Repos: deep-merge gripspace repos with local overrides (local scalar fields win,
    // collections like groups/linkfile/copyfile are unioned)
    for (name, gs_config) in merged_repos {
        match manifest.repos.entry(name) {
            std::collections::hash_map::Entry::Occupied(mut entry) => {
                // Local repo exists — deep-merge gripspace fields underneath
                let local = entry.get_mut();
                deep_merge_repo_config(local, &gs_config);
            }
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert(gs_config);
            }
        }
    }

    // Workspace: merge scripts, env, hooks
    let workspace = manifest
        .workspace
        .get_or_insert_with(WorkspaceConfig::default);

    // Scripts: gripspace scripts first, local overrides
    if !merged_scripts.is_empty() {
        let scripts = workspace.scripts.get_or_insert_with(HashMap::new);
        for (name, script) in merged_scripts {
            scripts.entry(name).or_insert(script);
        }
    }

    // Env: gripspace env first, local overrides
    if !merged_env.is_empty() {
        let env = workspace.env.get_or_insert_with(HashMap::new);
        for (key, value) in merged_env {
            env.entry(key).or_insert(value);
        }
    }

    // Hooks: concatenate (gripspace hooks run first, then local)
    if !merged_hooks_post_sync.is_empty() || !merged_hooks_post_checkout.is_empty() {
        let hooks = workspace.hooks.get_or_insert_with(WorkspaceHooks::default);

        if !merged_hooks_post_sync.is_empty() {
            let existing = hooks.post_sync.take().unwrap_or_default();
            merged_hooks_post_sync.extend(existing);
            hooks.post_sync = Some(merged_hooks_post_sync);
        }

        if !merged_hooks_post_checkout.is_empty() {
            let existing = hooks.post_checkout.take().unwrap_or_default();
            merged_hooks_post_checkout.extend(existing);
            hooks.post_checkout = Some(merged_hooks_post_checkout);
        }
    }

    // Agent config: gripspace agent config first, local overrides
    if let Some(gs_agent) = merged_agent {
        let local_agent = workspace.agent.take();
        let mut merged = gs_agent;
        if let Some(local) = local_agent {
            // Local fields win over gripspace fields
            if local.description.is_some() {
                merged.description = local.description;
            }
            if !local.conventions.is_empty() {
                // Prepend local conventions, then gripspace conventions
                let mut combined = local.conventions;
                for c in merged.conventions {
                    if !combined.contains(&c) {
                        combined.push(c);
                    }
                }
                merged.conventions = combined;
            }
            if let Some(local_workflows) = local.workflows {
                let workflows = merged.workflows.get_or_insert_with(HashMap::new);
                for (key, value) in local_workflows {
                    workflows.insert(key, value); // local wins
                }
            }
        }
        workspace.agent = Some(merged);
    }

    // Linkfiles from gripspaces are tracked separately — they'll be applied by the link command
    // We store them as manifest-level linkfiles, with local ones overriding by dest
    // Create the manifest config if it doesn't exist and there are gripspace files to merge
    if manifest.manifest.is_none() && (!merged_linkfiles.is_empty() || !merged_copyfiles.is_empty())
    {
        manifest.manifest = Some(crate::core::manifest::ManifestRepoConfig {
            url: String::new(),
            revision: None,
            copyfile: None,
            linkfile: None,
            composefile: None,
            platform: None,
        });
    }
    if let Some(ref mut manifest_config) = manifest.manifest {
        if !merged_linkfiles.is_empty() {
            let local_linkfiles = manifest_config.linkfile.take().unwrap_or_default();
            let local_dests: HashSet<String> =
                local_linkfiles.iter().map(|l| l.dest.clone()).collect();
            // Keep gripspace linkfiles that don't conflict with local
            let mut combined: Vec<_> = merged_linkfiles
                .into_iter()
                .filter(|l: &crate::core::manifest::LinkFileConfig| !local_dests.contains(&l.dest))
                .collect();
            combined.extend(local_linkfiles);
            if !combined.is_empty() {
                manifest_config.linkfile = Some(combined);
            }
        }

        if !merged_copyfiles.is_empty() {
            let local_copyfiles = manifest_config.copyfile.take().unwrap_or_default();
            let local_dests: HashSet<String> =
                local_copyfiles.iter().map(|c| c.dest.clone()).collect();
            let mut combined: Vec<_> = merged_copyfiles
                .into_iter()
                .filter(|c: &crate::core::manifest::CopyFileConfig| !local_dests.contains(&c.dest))
                .collect();
            combined.extend(local_copyfiles);
            if !combined.is_empty() {
                manifest_config.copyfile = Some(combined);
            }
        }
    }

    // Put gripspaces back (for status display and re-resolution on sync)
    manifest.gripspaces = Some(gripspaces);

    Ok(())
}

/// Recursively resolve a single gripspace and its nested gripspaces.
#[allow(clippy::too_many_arguments)]
fn resolve_gripspace_recursive(
    config: &GripspaceConfig,
    spaces_dir: &Path,
    active_stack: &mut HashSet<String>,
    resolved: &mut HashSet<String>,
    depth: usize,
    merged_repos: &mut HashMap<String, crate::core::manifest::RepoConfig>,
    merged_scripts: &mut HashMap<String, crate::core::manifest::WorkspaceScript>,
    merged_env: &mut HashMap<String, String>,
    merged_hooks_post_sync: &mut Vec<HookCommand>,
    merged_hooks_post_checkout: &mut Vec<HookCommand>,
    merged_linkfiles: &mut Vec<crate::core::manifest::LinkFileConfig>,
    merged_copyfiles: &mut Vec<crate::core::manifest::CopyFileConfig>,
    merged_agent: &mut Option<WorkspaceAgentConfig>,
) -> Result<(), ManifestError> {
    if depth >= MAX_GRIPSPACE_DEPTH {
        return Err(ManifestError::GripspaceError(format!(
            "Maximum gripspace include depth ({}) exceeded for '{}'",
            MAX_GRIPSPACE_DEPTH, config.url
        )));
    }

    let name = gripspace_name(&config.url);

    validate_space_name(&name)?;

    let identity_key = gripspace_identity(config);
    if resolved.contains(&identity_key) {
        return Ok(());
    }

    // Cycle detection uses normalized gripspace identity so distinct remotes
    // with the same basename (e.g., org1/foo vs org2/foo) don't false-positive.
    if !active_stack.insert(identity_key.clone()) {
        return Err(ManifestError::GripspaceError(format!(
            "Circular gripspace include detected: '{}' (from URL: '{}')",
            name, config.url
        )));
    }

    let gripspace_path = ensure_gripspace(spaces_dir, config)?;
    // Resolve the actual directory name (may differ from `name` due to reserved name suffixing)
    let dir_name = resolve_space_name(&config.url, spaces_dir)?;

    // Load the gripspace's manifest
    let Some(manifest_path) = manifest_paths::resolve_manifest_file_in_dir(&gripspace_path) else {
        active_stack.remove(&identity_key);
        return Err(ManifestError::GripspaceError(format!(
            "Gripspace '{}' has no gripspace manifest (expected gripspace.yml or manifest.yaml)",
            name
        )));
    };

    let gs_content = std::fs::read_to_string(&manifest_path).map_err(|e| {
        ManifestError::GripspaceError(format!(
            "Failed to read gripspace '{}' manifest: {}",
            name, e
        ))
    })?;
    let gs_manifest = Manifest::parse_raw(&gs_content)?;
    gs_manifest.validate_as_gripspace().map_err(|e| {
        ManifestError::GripspaceError(format!(
            "Gripspace '{}' manifest validation failed: {}",
            name, e
        ))
    })?;

    // Recursively resolve nested gripspaces first — ensure they are cloned
    if let Some(ref nested_gripspaces) = gs_manifest.gripspaces {
        for nested_config in nested_gripspaces {
            // Clone the nested gripspace if it doesn't exist yet
            ensure_gripspace(spaces_dir, nested_config)?;

            resolve_gripspace_recursive(
                nested_config,
                spaces_dir,
                active_stack,
                resolved,
                depth + 1,
                merged_repos,
                merged_scripts,
                merged_env,
                merged_hooks_post_sync,
                merged_hooks_post_checkout,
                merged_linkfiles,
                merged_copyfiles,
                merged_agent,
            )?;
        }
    }

    // Merge repos (first-encountered wins among gripspaces; local always wins last in resolve_all_gripspaces)
    for (repo_name, repo_config) in gs_manifest.repos {
        merged_repos.entry(repo_name).or_insert(repo_config);
    }

    // Merge workspace config
    if let Some(ref workspace) = gs_manifest.workspace {
        if let Some(ref scripts) = workspace.scripts {
            for (name, script) in scripts {
                merged_scripts
                    .entry(name.clone())
                    .or_insert_with(|| script.clone());
            }
        }

        if let Some(ref env) = workspace.env {
            for (key, value) in env {
                merged_env
                    .entry(key.clone())
                    .or_insert_with(|| value.clone());
            }
        }

        if let Some(ref hooks) = workspace.hooks {
            if let Some(ref post_sync) = hooks.post_sync {
                merged_hooks_post_sync.extend(post_sync.clone());
            }
            if let Some(ref post_checkout) = hooks.post_checkout {
                merged_hooks_post_checkout.extend(post_checkout.clone());
            }
        }

        if let Some(ref gs_agent_config) = workspace.agent {
            let agent = merged_agent.get_or_insert_with(WorkspaceAgentConfig::default);
            if agent.description.is_none() {
                agent.description.clone_from(&gs_agent_config.description);
            }
            for c in &gs_agent_config.conventions {
                if !agent.conventions.contains(c) {
                    agent.conventions.push(c.clone());
                }
            }
            if let Some(ref gs_workflows) = gs_agent_config.workflows {
                let workflows = agent.workflows.get_or_insert_with(HashMap::new);
                for (key, value) in gs_workflows {
                    workflows
                        .entry(key.clone())
                        .or_insert_with(|| value.clone());
                }
            }
        }
    }

    // Merge linkfiles and copyfiles from gripspace manifest config
    // These need path adjustment: source from .gitgrip/spaces/<dir_name>/
    if let Some(ref manifest_config) = gs_manifest.manifest {
        if let Some(ref linkfiles) = manifest_config.linkfile {
            for lf in linkfiles {
                merged_linkfiles.push(crate::core::manifest::LinkFileConfig {
                    // Prefix src with resolved dir name so link.rs knows where to find it
                    src: format!("gripspace:{}:{}", dir_name, lf.src),
                    dest: lf.dest.clone(),
                });
            }
        }
        if let Some(ref copyfiles) = manifest_config.copyfile {
            for cf in copyfiles {
                merged_copyfiles.push(crate::core::manifest::CopyFileConfig {
                    src: format!("gripspace:{}:{}", dir_name, cf.src),
                    dest: cf.dest.clone(),
                });
            }
        }
    }

    active_stack.remove(&identity_key);
    resolved.insert(identity_key);

    Ok(())
}

/// Get the current revision (branch or SHA) of a gripspace.
pub fn get_gripspace_rev(gripspace_path: &Path) -> Option<String> {
    let output = Command::new("git")
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(gripspace_path)
        .output()
        .ok()?;

    if output.status.success() {
        let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if branch == "HEAD" {
            // Detached HEAD — return SHA
            let output = Command::new("git")
                .args(["rev-parse", "--short", "HEAD"])
                .current_dir(gripspace_path)
                .output()
                .ok()?;
            Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
        } else {
            Some(branch)
        }
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn init_git_with_origin(repo_dir: &Path, origin: &str) {
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(repo_dir)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["remote", "add", "origin", origin])
            .current_dir(repo_dir)
            .output()
            .unwrap();
    }

    #[test]
    fn test_gripspace_name_https() {
        assert_eq!(
            gripspace_name("https://github.com/user/codi-gripspace.git"),
            "codi-gripspace"
        );
    }

    #[test]
    fn test_gripspace_name_ssh() {
        assert_eq!(
            gripspace_name("git@github.com:user/codi-gripspace.git"),
            "codi-gripspace"
        );
    }

    #[test]
    fn test_gripspace_name_no_extension() {
        assert_eq!(
            gripspace_name("https://github.com/user/my-space"),
            "my-space"
        );
    }

    #[test]
    fn test_gripspace_name_trailing_slash() {
        assert_eq!(
            gripspace_name("https://github.com/user/my-space/"),
            "my-space"
        );
    }

    #[test]
    fn test_resolve_no_gripspaces() {
        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let temp = tempfile::tempdir().unwrap();
        let result = resolve_all_gripspaces(&mut manifest, temp.path());
        assert!(result.is_ok());
    }

    #[test]
    fn test_resolve_empty_gripspaces() {
        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let temp = tempfile::tempdir().unwrap();
        let result = resolve_all_gripspaces(&mut manifest, temp.path());
        assert!(result.is_ok());
    }

    #[test]
    fn test_resolve_missing_gripspace_manifest() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path().join("spaces");
        // Create gripspace dir but no manifest file
        let test_dir = gripspaces_dir.join("test-gripspace");
        std::fs::create_dir_all(&test_dir).unwrap();
        init_git_with_origin(&test_dir, "https://github.com/user/test-gripspace.git");

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/test-gripspace.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, &gripspaces_dir);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("no gripspace manifest"));
    }

    #[test]
    fn test_resolve_merges_repos() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        // Create gripspace with a repo
        let gs_dir = gripspaces_dir.join("base-gripspace");
        std::fs::create_dir_all(&gs_dir).unwrap();
        init_git_with_origin(&gs_dir, "https://github.com/user/base-gripspace.git");
        std::fs::write(
            gs_dir.join("manifest.yaml"),
            r#"
version: 1
repos:
  shared-repo:
    url: https://github.com/user/shared.git
    path: ./shared
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/base-gripspace.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: {
                let mut m = HashMap::new();
                m.insert(
                    "local-repo".to_string(),
                    crate::core::manifest::RepoConfig {
                        url: Some("https://github.com/user/local.git".to_string()),
                        remote: None,
                        path: "./local".to_string(),
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
                    },
                );
                m
            },
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_ok());

        // Should have both repos
        assert_eq!(manifest.repos.len(), 2);
        assert!(manifest.repos.contains_key("shared-repo"));
        assert!(manifest.repos.contains_key("local-repo"));
    }

    #[test]
    fn test_resolve_local_repo_wins() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        // Create gripspace with a repo
        let gs_dir = gripspaces_dir.join("base-gripspace");
        std::fs::create_dir_all(&gs_dir).unwrap();
        init_git_with_origin(&gs_dir, "https://github.com/user/base-gripspace.git");
        std::fs::write(
            gs_dir.join("manifest.yaml"),
            r#"
version: 1
repos:
  my-repo:
    url: https://github.com/user/gripspace-version.git
    path: ./my-repo
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/base-gripspace.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: {
                let mut m = HashMap::new();
                m.insert(
                    "my-repo".to_string(),
                    crate::core::manifest::RepoConfig {
                        url: Some("https://github.com/user/local-version.git".to_string()),
                        remote: None,
                        path: "./my-repo-local".to_string(),
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
                    },
                );
                m
            },
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_ok());

        // Local repo should win
        assert_eq!(manifest.repos.len(), 1);
        let repo = manifest.repos.get("my-repo").unwrap();
        assert_eq!(
            repo.url,
            Some("https://github.com/user/local-version.git".to_string())
        );
        assert_eq!(repo.path, "./my-repo-local");
    }

    #[test]
    fn test_resolve_local_repo_deep_merges_gripspace_fields() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        // Create gripspace with a repo that has agent, groups, and linkfile
        let gs_dir = gripspaces_dir.join("rich-gripspace");
        std::fs::create_dir_all(&gs_dir).unwrap();
        init_git_with_origin(&gs_dir, "https://github.com/user/rich-gripspace.git");
        std::fs::write(
            gs_dir.join("manifest.yaml"),
            r#"
version: 1
repos:
  my-repo:
    url: https://github.com/user/gripspace-version.git
    path: ./my-repo
    revision: develop
    groups:
      - backend
      - shared
    agent:
      description: "My repo agent"
      language: typescript
      build: "npm run build"
      test: "npm test"
    linkfile:
      - src: config.yaml
        dest: my-repo-config.yaml
"#,
        )
        .unwrap();

        // Local manifest overrides only url and path (e.g., SSH instead of HTTPS)
        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/rich-gripspace.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: {
                let mut m = HashMap::new();
                m.insert(
                    "my-repo".to_string(),
                    crate::core::manifest::RepoConfig {
                        url: Some("git@github.com:user/local-version.git".to_string()),
                        remote: None,
                        path: "./my-repo".to_string(),
                        revision: None,
                        target: None,
                        sync_remote: None,
                        push_remote: None,
                        copyfile: None,
                        linkfile: None,
                        platform: None,
                        reference: false,
                        groups: vec!["local-group".to_string()],
                        agent: None,
                    },
                );
                m
            },
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_ok());

        let repo = manifest.repos.get("my-repo").unwrap();

        // Local scalar fields should win
        assert_eq!(
            repo.url,
            Some("git@github.com:user/local-version.git".to_string())
        );
        assert_eq!(repo.path, "./my-repo");

        // Gripspace revision should be inherited (local didn't set it)
        assert_eq!(repo.revision, Some("develop".to_string()));

        // Agent config should be inherited from gripspace (local didn't set it)
        assert!(repo.agent.is_some());
        let agent = repo.agent.as_ref().unwrap();
        assert_eq!(agent.description, Some("My repo agent".to_string()));
        assert_eq!(agent.language, Some("typescript".to_string()));
        assert_eq!(agent.build, Some("npm run build".to_string()));
        assert_eq!(agent.test, Some("npm test".to_string()));

        // Groups should be unioned (local + gripspace, deduplicated)
        assert!(repo.groups.contains(&"local-group".to_string()));
        assert!(repo.groups.contains(&"backend".to_string()));
        assert!(repo.groups.contains(&"shared".to_string()));

        // Linkfiles should be inherited from gripspace (local didn't set any)
        assert!(repo.linkfile.is_some());
        let linkfiles = repo.linkfile.as_ref().unwrap();
        assert_eq!(linkfiles.len(), 1);
        assert_eq!(linkfiles[0].dest, "my-repo-config.yaml");
    }

    #[test]
    fn test_deep_merge_repo_config_local_agent_wins() {
        let mut local = crate::core::manifest::RepoConfig {
            url: Some("git@github.com:user/local.git".to_string()),
            remote: None,
            path: "./repo".to_string(),
            revision: None,
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: None,
            linkfile: None,
            platform: None,
            reference: false,
            groups: Vec::new(),
            agent: Some(crate::core::manifest::RepoAgentConfig {
                description: Some("Local agent".to_string()),
                language: Some("rust".to_string()),
                build: None,
                test: None,
                lint: None,
                format: None,
            }),
        };

        let gripspace = crate::core::manifest::RepoConfig {
            url: Some("https://github.com/user/gs.git".to_string()),
            remote: None,
            path: "./repo".to_string(),
            revision: Some("main".to_string()),
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: None,
            linkfile: None,
            platform: None,
            reference: false,
            groups: vec!["gs-group".to_string()],
            agent: Some(crate::core::manifest::RepoAgentConfig {
                description: Some("GS agent".to_string()),
                language: Some("typescript".to_string()),
                build: Some("npm build".to_string()),
                test: Some("npm test".to_string()),
                lint: None,
                format: None,
            }),
        };

        deep_merge_repo_config(&mut local, &gripspace);

        // Local URL wins
        assert_eq!(local.url, Some("git@github.com:user/local.git".to_string()));
        // Gripspace revision inherited
        assert_eq!(local.revision, Some("main".to_string()));
        // Local agent wins entirely (it was set)
        let agent = local.agent.as_ref().unwrap();
        assert_eq!(agent.description, Some("Local agent".to_string()));
        assert_eq!(agent.language, Some("rust".to_string()));
        // Groups unioned
        assert!(local.groups.contains(&"gs-group".to_string()));
    }

    #[test]
    fn test_gripspace_name_empty_input() {
        // Edge case: empty or just .git should produce empty
        assert_eq!(gripspace_name(""), "");
        assert_eq!(gripspace_name(".git"), "");
    }

    #[test]
    fn test_ensure_gripspace_empty_name_fails() {
        let temp = tempfile::tempdir().unwrap();
        let config = GripspaceConfig {
            url: ".git".to_string(),
            rev: None,
        };
        let result = ensure_gripspace(temp.path(), &config);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("Invalid gripspace name"));
    }

    #[test]
    fn test_cycle_detection_normalized_name() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        // Create a gripspace that references itself (different URL, same name)
        let gs_dir = gripspaces_dir.join("self-ref");
        std::fs::create_dir_all(&gs_dir).unwrap();
        init_git_with_origin(&gs_dir, "https://github.com/user/self-ref.git");
        std::fs::write(
            gs_dir.join("manifest.yaml"),
            r#"
version: 1
gripspaces:
  - url: https://github.com/user/self-ref
repos:
  r:
    url: https://example.com/r.git
    path: ./r
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/self-ref.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Circular gripspace"));
    }

    #[test]
    fn test_max_depth_exceeded() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        // Create a chain of gripspaces that exceeds max depth
        // gs-0 -> gs-1 -> gs-2 -> gs-3 -> gs-4 -> gs-5 (exceeds MAX_GRIPSPACE_DEPTH=5)
        for i in 0..=5 {
            let gs_dir = gripspaces_dir.join(format!("gs-{}", i));
            std::fs::create_dir_all(&gs_dir).unwrap();
            init_git_with_origin(&gs_dir, &format!("https://github.com/user/gs-{}.git", i));
            let next_gs = if i < 5 {
                format!(
                    r#"
gripspaces:
  - url: https://github.com/user/gs-{}.git
"#,
                    i + 1
                )
            } else {
                String::new()
            };
            std::fs::write(
                gs_dir.join("manifest.yaml"),
                format!(
                    r#"
version: 1
{}
repos:
  r{}:
    url: https://example.com/r{}.git
    path: ./r{}
"#,
                    next_gs, i, i, i
                ),
            )
            .unwrap();
        }

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/gs-0.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Maximum gripspace include depth"));
    }

    #[test]
    fn test_resolve_merges_scripts() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        let gs_dir = gripspaces_dir.join("base-gripspace");
        std::fs::create_dir_all(&gs_dir).unwrap();
        init_git_with_origin(&gs_dir, "https://github.com/user/base-gripspace.git");
        std::fs::write(
            gs_dir.join("manifest.yaml"),
            r#"
version: 1
repos:
  shared:
    url: https://github.com/user/shared.git
    path: ./shared
workspace:
  scripts:
    build:
      command: "echo build from gripspace"
      description: "Build from gripspace"
    test:
      command: "echo test from gripspace"
      description: "Test from gripspace"
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/user/base-gripspace.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: Some(WorkspaceConfig {
                scripts: Some({
                    let mut m = HashMap::new();
                    m.insert(
                        "build".to_string(),
                        crate::core::manifest::WorkspaceScript {
                            command: Some("echo local build".to_string()),
                            description: Some("Local build".to_string()),
                            cwd: None,
                            steps: None,
                        },
                    );
                    m
                }),
                env: None,
                hooks: None,
                ci: None,
                agent: None,
                release: None,
            }),
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_ok());

        let scripts = manifest
            .workspace
            .as_ref()
            .unwrap()
            .scripts
            .as_ref()
            .unwrap();
        // Local "build" should win
        assert_eq!(
            scripts.get("build").unwrap().command.as_deref(),
            Some("echo local build")
        );
        // Gripspace "test" should be inherited
        assert!(scripts.contains_key("test"));
        assert_eq!(
            scripts.get("test").unwrap().command.as_deref(),
            Some("echo test from gripspace")
        );
    }

    #[test]
    fn test_resolve_shared_nested_include_not_treated_as_cycle() {
        let temp = tempfile::tempdir().unwrap();
        let gripspaces_dir = temp.path();

        for name in ["a", "b", "c", "d"] {
            std::fs::create_dir_all(gripspaces_dir.join(name)).unwrap();
        }
        init_git_with_origin(&gripspaces_dir.join("a"), "https://github.com/org/a.git");
        init_git_with_origin(&gripspaces_dir.join("b"), "https://github.com/org/b.git");
        init_git_with_origin(&gripspaces_dir.join("c"), "https://github.com/org/c.git");
        init_git_with_origin(&gripspaces_dir.join("d"), "https://github.com/org/d.git");

        // A -> [B, C], B -> [D], C -> [D] should be valid (DAG, not a cycle).
        std::fs::write(
            gripspaces_dir.join("a").join("gripspace.yml"),
            r#"
version: 1
gripspaces:
  - url: https://github.com/org/b.git
  - url: https://github.com/org/c.git
repos:
  a-repo:
    url: https://github.com/org/a-repo.git
    path: ./a-repo
"#,
        )
        .unwrap();

        std::fs::write(
            gripspaces_dir.join("b").join("gripspace.yml"),
            r#"
version: 1
gripspaces:
  - url: https://github.com/org/d.git
repos:
  b-repo:
    url: https://github.com/org/b-repo.git
    path: ./b-repo
"#,
        )
        .unwrap();

        std::fs::write(
            gripspaces_dir.join("c").join("gripspace.yml"),
            r#"
version: 1
gripspaces:
  - url: https://github.com/org/d.git
repos:
  c-repo:
    url: https://github.com/org/c-repo.git
    path: ./c-repo
"#,
        )
        .unwrap();

        std::fs::write(
            gripspaces_dir.join("d").join("gripspace.yml"),
            r#"
version: 1
repos:
  d-repo:
    url: https://github.com/org/d-repo.git
    path: ./d-repo
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/org/a.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, gripspaces_dir);
        assert!(result.is_ok(), "{}", result.unwrap_err());
        assert!(manifest.repos.contains_key("a-repo"));
        assert!(manifest.repos.contains_key("b-repo"));
        assert!(manifest.repos.contains_key("c-repo"));
        assert!(manifest.repos.contains_key("d-repo"));
    }

    #[test]
    fn test_nested_distinct_remotes_same_basename_not_cycle() {
        let temp = tempfile::tempdir().unwrap();
        let spaces_dir = temp.path();

        // root-space includes two different remotes that both end in "common.git".
        std::fs::create_dir_all(spaces_dir.join("root-space")).unwrap();
        std::fs::create_dir_all(spaces_dir.join("common")).unwrap();
        std::fs::create_dir_all(spaces_dir.join("common-2")).unwrap();

        init_git_with_origin(
            &spaces_dir.join("root-space"),
            "https://github.com/root/root-space.git",
        );
        init_git_with_origin(
            &spaces_dir.join("common"),
            "https://github.com/org1/common.git",
        );
        init_git_with_origin(
            &spaces_dir.join("common-2"),
            "https://github.com/org2/common.git",
        );

        std::fs::write(
            spaces_dir.join("root-space").join("gripspace.yml"),
            r#"
version: 1
gripspaces:
  - url: https://github.com/org1/common.git
  - url: https://github.com/org2/common.git
repos:
  root-repo:
    url: https://github.com/root/repo.git
    path: ./root
"#,
        )
        .unwrap();

        std::fs::write(
            spaces_dir.join("common").join("gripspace.yml"),
            r#"
version: 1
repos:
  common-one:
    url: https://github.com/org1/repo.git
    path: ./one
"#,
        )
        .unwrap();

        std::fs::write(
            spaces_dir.join("common-2").join("gripspace.yml"),
            r#"
version: 1
repos:
  common-two:
    url: https://github.com/org2/repo.git
    path: ./two
"#,
        )
        .unwrap();

        let mut manifest = Manifest {
            version: 1,
            remotes: None,
            gripspaces: Some(vec![GripspaceConfig {
                url: "https://github.com/root/root-space.git".to_string(),
                rev: None,
            }]),
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = resolve_all_gripspaces(&mut manifest, spaces_dir);
        assert!(result.is_ok(), "{}", result.unwrap_err());
        assert!(manifest.repos.contains_key("root-repo"));
        assert!(manifest.repos.contains_key("common-one"));
        assert!(manifest.repos.contains_key("common-two"));
    }

    #[test]
    fn test_resolve_space_name_normal() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();
        let name = resolve_space_name("https://github.com/user/my-gripspace.git", spaces).unwrap();
        assert_eq!(name, "my-gripspace");
    }

    #[test]
    fn test_resolve_space_name_rejects_dotdot() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();
        let err = resolve_space_name("https://github.com/user/..", spaces).unwrap_err();
        assert!(err.to_string().contains("Invalid gripspace name"));
    }

    #[test]
    fn test_resolve_space_name_rejects_invalid_characters() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();
        let err =
            resolve_space_name("https://github.com/user/my gripspace.git", spaces).unwrap_err();
        assert!(err.to_string().contains("Invalid gripspace name"));
    }

    #[test]
    fn test_resolve_space_name_reserved_main() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();
        // "main" is reserved — should auto-suffix to "main-1"
        let name = resolve_space_name("https://github.com/user/main.git", spaces).unwrap();
        assert_eq!(name, "main-1");
    }

    #[test]
    fn test_resolve_space_name_reserved_local() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();
        let name = resolve_space_name("https://github.com/user/local.git", spaces).unwrap();
        assert_eq!(name, "local-1");
    }

    #[test]
    fn test_resolve_space_name_duplicate_increments() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();

        // Create an existing dir with a different remote
        let existing = spaces.join("my-repo");
        std::fs::create_dir_all(&existing).unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&existing)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/other/my-repo.git",
            ])
            .current_dir(&existing)
            .output()
            .unwrap();

        // A different URL producing the same name should auto-increment
        let name = resolve_space_name("https://github.com/user/my-repo.git", spaces).unwrap();
        assert_eq!(name, "my-repo-2");
    }

    #[test]
    fn test_resolve_space_name_same_remote_reuses() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();

        // Create an existing dir with the same remote
        let existing = spaces.join("my-repo");
        std::fs::create_dir_all(&existing).unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&existing)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/user/my-repo.git",
            ])
            .current_dir(&existing)
            .output()
            .unwrap();

        // Same URL should reuse the existing directory name
        let name = resolve_space_name("https://github.com/user/my-repo.git", spaces).unwrap();
        assert_eq!(name, "my-repo");
    }

    #[test]
    fn test_resolve_space_name_non_git_dir_does_not_reuse() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();

        // Existing folder without git metadata should not be reused.
        let existing = spaces.join("my-repo");
        std::fs::create_dir_all(&existing).unwrap();
        std::fs::write(existing.join("README.md"), "local").unwrap();

        let name = resolve_space_name("https://github.com/user/my-repo.git", spaces).unwrap();
        assert_eq!(name, "my-repo-2");
    }

    #[test]
    fn test_resolve_space_name_git_dir_without_origin_does_not_reuse() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();

        let existing = spaces.join("my-repo");
        std::fs::create_dir_all(&existing).unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&existing)
            .output()
            .unwrap();

        let name = resolve_space_name("https://github.com/user/my-repo.git", spaces).unwrap();
        assert_eq!(name, "my-repo-2");
    }

    #[test]
    fn test_resolve_space_name_reuses_equivalent_ssh_https_remote() {
        let temp = tempfile::tempdir().unwrap();
        let spaces = temp.path();

        let existing = spaces.join("my-repo");
        std::fs::create_dir_all(&existing).unwrap();
        init_git_with_origin(&existing, "git@github.com:user/my-repo.git");

        let name = resolve_space_name("https://github.com/user/my-repo.git", spaces).unwrap();
        assert_eq!(name, "my-repo");
    }

    #[test]
    fn test_checkout_rev_rejects_flags() {
        // checkout_rev is private, so test via ensure_gripspace with a rev that looks like a flag
        let temp = tempfile::tempdir().unwrap();
        let gs_dir = temp.path().join("test-repo");
        std::fs::create_dir_all(&gs_dir).unwrap();
        // Initialize a bare git repo so the path exists
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&gs_dir)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/user/test-repo.git",
            ])
            .current_dir(&gs_dir)
            .output()
            .unwrap();

        let config = GripspaceConfig {
            url: "https://github.com/user/test-repo.git".to_string(),
            rev: Some("--orphan".to_string()),
        };
        let result = ensure_gripspace(temp.path(), &config);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Invalid rev"));
    }

    #[test]
    fn test_checkout_rev_rejects_whitespace() {
        let temp = tempfile::tempdir().unwrap();
        let gs_dir = temp.path().join("test-repo");
        std::fs::create_dir_all(&gs_dir).unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&gs_dir)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/user/test-repo.git",
            ])
            .current_dir(&gs_dir)
            .output()
            .unwrap();

        let config = GripspaceConfig {
            url: "https://github.com/user/test-repo.git".to_string(),
            rev: Some("main ; rm -rf /".to_string()),
        };
        let result = ensure_gripspace(temp.path(), &config);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Invalid rev"));
    }

    #[test]
    fn test_checkout_rev_rejects_empty() {
        let temp = tempfile::tempdir().unwrap();
        let gs_dir = temp.path().join("test-repo");
        std::fs::create_dir_all(&gs_dir).unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&gs_dir)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/user/test-repo.git",
            ])
            .current_dir(&gs_dir)
            .output()
            .unwrap();

        let config = GripspaceConfig {
            url: "https://github.com/user/test-repo.git".to_string(),
            rev: Some("".to_string()),
        };
        let result = ensure_gripspace(temp.path(), &config);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Invalid rev"));
    }

    // ── normalize_url tests ────────────────────────────────────────────

    #[test]
    fn test_normalize_url_ssh_scp() {
        assert_eq!(
            normalize_url("git@github.com:user/repo.git"),
            "github.com:user/repo"
        );
    }

    #[test]
    fn test_normalize_url_https() {
        assert_eq!(
            normalize_url("https://github.com/user/repo.git"),
            "github.com:user/repo"
        );
    }

    #[test]
    fn test_normalize_url_ssh_scheme() {
        assert_eq!(
            normalize_url("ssh://git@github.com/user/repo.git"),
            "github.com:user/repo"
        );
    }

    #[test]
    fn test_normalize_url_trailing_slash() {
        assert_eq!(
            normalize_url("https://github.com/user/repo/"),
            "github.com:user/repo"
        );
    }

    #[test]
    fn test_normalize_url_case_insensitive_host() {
        assert_eq!(
            normalize_url("https://GitHub.COM/user/repo"),
            "github.com:user/repo"
        );
    }

    #[test]
    fn test_normalize_url_ssh_and_https_equivalent() {
        let ssh = normalize_url("git@github.com:user/repo.git");
        let https = normalize_url("https://github.com/user/repo.git");
        assert_eq!(ssh, https);
    }

    // ── gripspace_identity tests ───────────────────────────────────────

    #[test]
    fn test_gripspace_identity_no_rev() {
        let config = GripspaceConfig {
            url: "https://github.com/user/repo.git".to_string(),
            rev: None,
        };
        let identity = gripspace_identity(&config);
        assert_eq!(identity, "github.com:user/repo");
    }

    #[test]
    fn test_gripspace_identity_with_rev() {
        let config = GripspaceConfig {
            url: "https://github.com/user/repo.git".to_string(),
            rev: Some("v1.0".to_string()),
        };
        let identity = gripspace_identity(&config);
        assert_eq!(identity, "github.com:user/repo#v1.0");
    }

    #[test]
    fn test_gripspace_identity_empty_rev() {
        let config = GripspaceConfig {
            url: "https://github.com/user/repo.git".to_string(),
            rev: Some("".to_string()),
        };
        // Empty rev should be treated as no rev
        let identity = gripspace_identity(&config);
        assert_eq!(identity, "github.com:user/repo");
    }

    // ── validate_space_name tests ──────────────────────────────────────

    #[test]
    fn test_validate_space_name_valid() {
        assert!(validate_space_name("my-gripspace").is_ok());
        assert!(validate_space_name("my_space.v2").is_ok());
        assert!(validate_space_name("A123").is_ok());
    }

    #[test]
    fn test_validate_space_name_rejects_dot() {
        assert!(validate_space_name(".").is_err());
    }

    #[test]
    fn test_validate_space_name_rejects_dotdot() {
        assert!(validate_space_name("..").is_err());
    }

    #[test]
    fn test_validate_space_name_rejects_slashes() {
        assert!(validate_space_name("a/b").is_err());
    }

    #[test]
    fn test_validate_space_name_rejects_spaces() {
        assert!(validate_space_name("my space").is_err());
    }

    #[test]
    fn test_validate_space_name_rejects_embedded_dotdot() {
        assert!(validate_space_name("a..b").is_err());
    }
}
