//! `gr migrate` — convert existing repos into a gripspace (#424).
//!
//! Two subcommands:
//!   - `from-repos`: Generate a new gripspace from GitHub repo list
//!   - `in-place`:   Convert an existing git repo dir into a gripspace

use crate::cli::output::Output;
use crate::core::griptree::{GriptreeConfig, GriptreePointer, GriptreeRepoInfo};
use crate::core::manifest::{CloneStrategy, Manifest, ManifestSettings, MergeStrategy, RepoConfig};
use crate::core::manifest_paths;
use chrono::Utc;
use dialoguer::{theme::ColorfulTheme, Confirm, Input, Select};
use std::collections::{HashMap, HashSet};
use std::io::IsTerminal;
use std::path::{Path, PathBuf};
use std::process::Output as ProcessOutput;

/// Agent configuration collected during interactive setup.
struct AgentSpec {
    name: String,
    role: String,
    model: String,
    tool: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ExistingWorktree {
    root: PathBuf,
    branch: Option<String>,
    is_main: bool,
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
struct MigratedGriptreesList {
    griptrees: HashMap<String, MigratedGriptreeEntry>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct MigratedGriptreeEntry {
    path: String,
    branch: String,
    locked: bool,
    lock_reason: Option<String>,
}

/// Run `gr migrate from-repos` — generate gripspace from GitHub repos.
pub async fn run_migrate_from_repos(
    repos: &[String],
    org: Option<&str>,
    prefix: Option<&str>,
    path: Option<&str>,
    json: bool,
) -> anyhow::Result<()> {
    if repos.is_empty() {
        anyhow::bail!("At least one --repo is required");
    }

    if !json {
        Output::header("Migrating repos to gripspace...");
        println!();
    }

    // Parse repo specs (owner/name)
    let mut parsed_repos: Vec<(String, String)> = Vec::new();
    for spec in repos {
        let parts: Vec<&str> = spec.splitn(2, '/').collect();
        if parts.len() != 2 {
            anyhow::bail!(
                "Invalid repo format '{}': expected owner/repo (e.g. GetConversa/conversa-app)",
                spec
            );
        }
        parsed_repos.push((parts[0].to_string(), parts[1].to_string()));
    }

    // Determine target directory
    let target_dir = match path {
        Some(p) => PathBuf::from(p),
        None => {
            let name = prefix.unwrap_or("gripspace");
            std::env::current_dir()?.join(name)
        }
    };

    let prefix_str = prefix.unwrap_or("workspace");
    let org_str = org.unwrap_or_else(|| parsed_repos[0].0.as_str());

    if !json {
        Output::info(&format!("Target directory: {}", target_dir.display()));
        Output::info(&format!("Repos to migrate: {}", repos.len()));
        println!();
    }

    // Interactive agent team configuration
    let interactive = !json && std::io::stdin().is_terminal();
    let agents = if interactive {
        configure_agents_interactive()?
    } else {
        Vec::new()
    };

    let premium = if interactive {
        let theme = ColorfulTheme::default();
        Confirm::with_theme(&theme)
            .with_prompt("Enable premium features (persistent agents, team sharing)?")
            .default(false)
            .interact()?
    } else {
        false
    };

    // Generate all files
    let manifest_yaml = generate_manifest_yaml(&parsed_repos, org_str, prefix_str);
    let claude_md = generate_claude_md(&parsed_repos, prefix_str, &agents, premium);
    let agents_toml = generate_agents_toml(prefix_str, &agents);
    let prompts: Vec<(String, String)> = agents
        .iter()
        .map(|a| {
            (
                a.name.clone(),
                generate_agent_prompt(a, &parsed_repos, prefix_str),
            )
        })
        .collect();

    // Create directory structure
    create_gripspace_dirs(
        &target_dir,
        &manifest_yaml,
        &claude_md,
        &agents_toml,
        &prompts,
    )?;

    if !json {
        Output::success("Gripspace structure created:");
        println!("  {}/", target_dir.display());
        println!("    .gitgrip/spaces/main/gripspace.yml");
        println!("    config/CLAUDE.md");
        println!("    config/agents.toml");
        for (name, _) in &prompts {
            println!("    config/prompts/{}.md", name);
        }
        println!();
        Output::info("Next steps:");
        println!("  1. cd {}", target_dir.display());
        println!("  2. Review and edit .gitgrip/spaces/main/gripspace.yml");
        println!("  3. gr sync   # Clone all repos");
        println!("  4. gr status # Verify everything works");
        if !agents.is_empty() {
            println!("  5. gr spawn up  # Launch agent team");
        }
    }

    if json {
        let result = serde_json::json!({
            "target_dir": target_dir.display().to_string(),
            "repos": repos,
            "agents": agents.iter().map(|a| &a.name).collect::<Vec<_>>(),
            "premium": premium,
            "manifest": ".gitgrip/spaces/main/gripspace.yml",
            "config": "config/",
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

/// Interactive agent team configuration using dialoguer.
fn configure_agents_interactive() -> anyhow::Result<Vec<AgentSpec>> {
    let theme = ColorfulTheme::default();
    let mut agents = Vec::new();

    let add_agents = Confirm::with_theme(&theme)
        .with_prompt("Configure AI agent team?")
        .default(true)
        .interact()?;

    if !add_agents {
        return Ok(agents);
    }

    println!();
    Output::info("Add agents one at a time. Press Enter with empty name to finish.");
    println!();

    let models = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"];
    let tools = ["claude", "codex"];

    loop {
        let name: String = Input::with_theme(&theme)
            .with_prompt("Agent name (empty to finish)")
            .allow_empty(true)
            .interact_text()?;

        if name.is_empty() {
            break;
        }

        let role: String = Input::with_theme(&theme)
            .with_prompt("Role")
            .default("implementation".to_string())
            .interact_text()?;

        let model_idx = Select::with_theme(&theme)
            .with_prompt("Model")
            .items(&models)
            .default(1) // sonnet
            .interact()?;

        let tool_idx = Select::with_theme(&theme)
            .with_prompt("Tool")
            .items(&tools)
            .default(0) // claude
            .interact()?;

        agents.push(AgentSpec {
            name,
            role,
            model: models[model_idx].to_string(),
            tool: tools[tool_idx].to_string(),
        });

        println!();
    }

    Ok(agents)
}

/// Generate gripspace.yml content.
fn generate_manifest_yaml(repos: &[(String, String)], org: &str, prefix: &str) -> String {
    let mut yaml = String::new();
    yaml.push_str("# Generated by gr migrate from-repos\n");
    yaml.push_str("version: 2\n\n");

    // Manifest self-reference
    yaml.push_str("manifest:\n");
    yaml.push_str(&format!(
        "  url: https://github.com/{}/{}-gripspace.git\n",
        org, prefix
    ));
    yaml.push_str("  revision: main\n\n");

    // Repos
    yaml.push_str("repos:\n");
    for (owner, name) in repos {
        yaml.push_str(&format!("  {}:\n", name));
        yaml.push_str(&format!(
            "    url: https://github.com/{}/{}.git\n",
            owner, name
        ));
        yaml.push_str(&format!("    path: ./{}\n", name));
        yaml.push_str("    revision: main\n\n");
    }

    // Config repo — linkfiles source CLAUDE.md and agents.toml into workspace root
    yaml.push_str(&format!("  {}-config:\n", prefix));
    yaml.push_str(&format!(
        "    url: https://github.com/{}/{}-config.git\n",
        org, prefix
    ));
    yaml.push_str("    path: ./config\n");
    yaml.push_str("    revision: main\n");
    yaml.push_str("    linkfile:\n");
    yaml.push_str("      - src: CLAUDE.md\n");
    yaml.push_str("        dest: CLAUDE.md\n");
    yaml.push_str("      - src: agents.toml\n");
    yaml.push_str("        dest: .gitgrip/agents.toml\n\n");

    // Settings
    yaml.push_str("settings:\n");
    yaml.push_str("  revision: main\n");
    yaml.push_str("  clone_strategy: clone\n");

    yaml
}

/// Generate CLAUDE.md with repo table and agent context.
fn generate_claude_md(
    repos: &[(String, String)],
    prefix: &str,
    agents: &[AgentSpec],
    premium: bool,
) -> String {
    let mut md = String::new();
    md.push_str(&format!("# {}\n\n", prefix));
    md.push_str("Multi-repo workspace managed by **gitgrip** (`gr`). Always use `gr` — never raw `git` or `gh`.\n\n");

    md.push_str("## Repos\n\n");
    md.push_str("| Repo | Path | Description |\n");
    md.push_str("|------|------|-------------|\n");
    for (_owner, name) in repos {
        md.push_str(&format!("| **{}** | `{}/` | |\n", name, name));
    }

    if !agents.is_empty() {
        md.push_str("\n## Agent Team\n\n");
        md.push_str("| Agent | Role | Model |\n");
        md.push_str("|-------|------|-------|\n");
        for agent in agents {
            md.push_str(&format!(
                "| **{}** | {} | {} |\n",
                agent.name, agent.role, agent.model
            ));
        }
    }

    md.push_str("\n## Git Workflow\n\n");
    md.push_str("Always use `gr` instead of `git` or `gh`.\n\n");
    md.push_str("```bash\n");
    md.push_str("gr sync                  # Pull all repos\n");
    md.push_str("gr status                # Check state\n");
    md.push_str("gr branch feat/my-feat   # Branch across repos\n");
    md.push_str("gr add . && gr commit -m \"message\" && gr push -u\n");
    md.push_str("gr pr create -t \"feat: title\" --push\n");
    md.push_str("gr pr merge              # Merge linked PRs\n");
    md.push_str("```\n");

    if premium {
        md.push_str("\n## Premium Features\n\n");
        md.push_str("This workspace has premium features enabled:\n");
        md.push_str("- `recall_identity` — persistent agent identity\n");
        md.push_str("- `recall_career` — cross-project career memory\n");
        md.push_str("- `recall_promote` — share knowledge across team\n");
        md.push_str("- `recall_approve` — approve promoted knowledge\n\n");
        md.push_str("Use `recall_promote` after learning something reusable across projects.\n");
    }

    md
}

/// Generate agents.toml with full team configuration.
fn generate_agents_toml(prefix: &str, agents: &[AgentSpec]) -> String {
    let mut toml = String::new();
    toml.push_str(&format!("# Agent configuration for {}\n\n", prefix));
    toml.push_str("[spawn]\n");
    toml.push_str(&format!("session_name = \"{}\"\n", prefix));
    toml.push_str("channel = \"dev\"\n");
    toml.push_str("auto_journal = true\n\n");

    if agents.is_empty() {
        toml.push_str("# Add agents here:\n");
        toml.push_str("# [agents.my-agent]\n");
        toml.push_str("# role = \"implementation\"\n");
        toml.push_str("# model = \"claude-sonnet-4-6\"\n");
        toml.push_str("# tool = \"claude\"\n");
        toml.push_str("# worktree = \"main\"\n");
    } else {
        for agent in agents {
            toml.push_str(&format!("[agents.{}]\n", agent.name));
            toml.push_str(&format!("role = \"{}\"\n", agent.role));
            toml.push_str(&format!("model = \"{}\"\n", agent.model));
            toml.push_str(&format!("tool = \"{}\"\n", agent.tool));
            toml.push_str("worktree = \"main\"\n");
            toml.push_str(&format!(
                "startup_prompt = \"config/prompts/{}.md\"\n",
                agent.name
            ));
            toml.push_str("channel = \"dev\"\n");
            toml.push_str("loop_interval = \"2m\"\n\n");
        }
    }

    toml
}

/// Generate a per-agent prompt file.
fn generate_agent_prompt(agent: &AgentSpec, repos: &[(String, String)], prefix: &str) -> String {
    let mut prompt = String::new();
    prompt.push_str(&format!("# {} — {}\n\n", agent.name, agent.role));
    prompt.push_str(&format!(
        "You are **{}**, an AI agent working on the **{}** project.\n\n",
        agent.name, prefix
    ));
    prompt.push_str(&format!("## Your Role\n\n{}\n\n", agent.role));
    prompt.push_str("## Workspace\n\n");
    prompt.push_str("This is a multi-repo workspace managed by `gr` (gitgrip). Repos:\n\n");
    for (_owner, name) in repos {
        prompt.push_str(&format!("- `{}/`\n", name));
    }
    prompt.push_str("\n## Rules\n\n");
    prompt.push_str("- Always use `gr` for git operations, never raw `git` or `gh`\n");
    prompt.push_str("- Check #dev channel for assignments before starting work\n");
    prompt.push_str("- Claim tasks before building\n");
    prompt.push_str("- Post intent before coding\n");
    prompt.push_str("- Tests on first submission\n");
    prompt
}

/// Create the gripspace directory structure on disk.
fn create_gripspace_dirs(
    target: &Path,
    manifest_yaml: &str,
    claude_md: &str,
    agents_toml: &str,
    prompts: &[(String, String)],
) -> anyhow::Result<()> {
    let manifest_dir = target.join(".gitgrip/spaces/main");
    let config_dir = target.join("config");
    let prompts_dir = config_dir.join("prompts");
    std::fs::create_dir_all(&manifest_dir)?;
    std::fs::create_dir_all(&prompts_dir)?;

    // Write files
    std::fs::write(manifest_dir.join("gripspace.yml"), manifest_yaml)?;
    std::fs::write(config_dir.join("CLAUDE.md"), claude_md)?;
    std::fs::write(config_dir.join("agents.toml"), agents_toml)?;

    // Write per-agent prompts
    for (name, content) in prompts {
        std::fs::write(prompts_dir.join(format!("{}.md", name)), content)?;
    }

    // Initialize manifest directory as a git repo so gr init can use it
    let git_init = std::process::Command::new("git")
        .args(["init", "-b", "main"])
        .current_dir(&manifest_dir)
        .output();
    if let Err(e) = git_init {
        Output::warning(&format!("git init failed: {}", e));
    }
    let git_add = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(&manifest_dir)
        .output();
    if let Err(e) = git_add {
        Output::warning(&format!("git add failed: {}", e));
    }
    let git_commit = std::process::Command::new("git")
        .args(["commit", "-m", "Initial gripspace manifest"])
        .current_dir(&manifest_dir)
        .output();
    if let Err(e) = git_commit {
        Output::warning(&format!("git commit failed: {}", e));
    }

    Ok(())
}

/// Run `gr migrate in-place` — convert a git repo dir into a gripspace.
///
/// Algorithm (v3.2):
/// 1. Derive repo name from `git remote get-url origin` (basename, strip .git)
///    Falls back to directory name if no remote or command fails.
/// 2. Enumerate the main repo plus linked worktrees via `git worktree list --porcelain`.
/// 3. For each worktree root, move everything into `<worktree-root>/<repo-name>` EXCEPT:
///    .synapt/, .claude/, .env, _migrate_tmp/, and the child dir name.
/// 4. Run `git worktree repair` after all paths move so linked worktree metadata points at
///    the new `<worktree-root>/<repo-name>` locations.
/// 5. Create `.gitgrip/griptrees.json` at the main root and `.griptree` /
///    `.gitgrip/griptree.json` in each linked worktree root.
pub async fn run_migrate_in_place(
    path: Option<&str>,
    dry_run: bool,
    json: bool,
) -> anyhow::Result<()> {
    let root = match path {
        Some(p) => PathBuf::from(p).canonicalize()?,
        None => std::env::current_dir()?,
    };

    // Guard: must be a git repo (has .git at root)
    if !root.join(".git").exists() {
        if root.join(".gitgrip").exists() {
            anyhow::bail!(
                "Already a gripspace (has .gitgrip/). Run `gr status` to check the workspace."
            );
        }
        anyhow::bail!("Not a git repository: no .git found at {}", root.display());
    }

    // Guard: git 2.30+ required for `git worktree repair`
    check_git_version_for_repair()?;

    // Derive repo name
    let repo_name = derive_repo_name_from_remote(&root);
    let worktrees = list_existing_worktrees(&root)?;
    let linked_worktrees: Vec<ExistingWorktree> =
        worktrees.iter().filter(|wt| !wt.is_main).cloned().collect();
    let main_revision = worktrees
        .iter()
        .find(|wt| wt.is_main)
        .and_then(|wt| wt.branch.clone())
        .unwrap_or_else(|| "main".to_string());

    validate_migration_targets(&worktrees, &repo_name)?;

    if !json {
        Output::header("gr migrate in-place");
        println!();
        Output::info(&format!("Gripspace root: {}", root.display()));
        Output::info(&format!("Repo child:     {}/{}", root.display(), repo_name));
        if !linked_worktrees.is_empty() {
            Output::info(&format!("Linked worktrees: {}", linked_worktrees.len()));
            for wt in &linked_worktrees {
                let branch = wt.branch.as_deref().unwrap_or("(detached)");
                println!("  {} -> {}/{}", branch, wt.root.display(), repo_name);
            }
        }
        println!();
        if dry_run {
            Output::warning("DRY RUN — no changes will be made");
            println!();
        }
    }

    if !json && dry_run {
        for wt in &worktrees {
            let to_move = collect_paths_to_move(&wt.root, &repo_name)?;
            let label = if wt.is_main {
                "main repo".to_string()
            } else {
                format!(
                    "linked worktree ({})",
                    wt.branch.as_deref().unwrap_or("detached")
                )
            };
            println!(
                "  Would move {} into {}/{}:",
                label,
                wt.root.display(),
                repo_name
            );
            for p in &to_move {
                println!(
                    "    {}",
                    p.file_name().unwrap_or_default().to_string_lossy()
                );
            }
            println!();
            println!("  Would keep at {}:", wt.root.display());
            for path in [".synapt", ".claude", ".env"] {
                if wt.root.join(path).exists() {
                    if wt.root.join(path).is_dir() {
                        println!("    {}/", path);
                    } else {
                        println!("    {}", path);
                    }
                }
            }
            println!();
        }
        println!("  Would run: git worktree repair");
        println!("  Would create: {}/.gitgrip/griptrees.json", root.display());
        println!(
            "  Would create: {}/.gitgrip/spaces/main/gripspace.yml",
            root.display()
        );
        for wt in &linked_worktrees {
            println!("  Would create: {}/.griptree", wt.root.display());
            println!(
                "  Would create: {}/.gitgrip/griptree.json",
                wt.root.display()
            );
        }
        return Ok(());
    }

    if dry_run {
        let result = serde_json::json!({
            "root": root.display().to_string(),
            "repo_name": repo_name,
            "worktrees": worktrees.iter().map(|wt| serde_json::json!({
                "root": wt.root.display().to_string(),
                "branch": wt.branch,
                "is_main": wt.is_main,
                "to_move": collect_paths_to_move(&wt.root, &repo_name).unwrap_or_default().iter()
                    .map(|p| p.file_name().unwrap_or_default().to_string_lossy().to_string())
                    .collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
            "dry_run": true,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    for wt in &worktrees {
        migrate_single_worktree_root(&wt.root, &repo_name)?;
    }

    let repair_outputs = run_worktree_repairs(&worktrees, &repo_name)?;
    for repair in &repair_outputs {
        if !json {
            let repair_out = String::from_utf8_lossy(&repair.stdout);
            let repair_err = String::from_utf8_lossy(&repair.stderr);
            if !repair_out.trim().is_empty() || !repair_err.trim().is_empty() {
                Output::info("git worktree repair output:");
                for line in repair_out.lines().chain(repair_err.lines()) {
                    println!("  {}", line);
                }
            }
        }
        worktree_repair_must_succeed(repair)?;
    }

    create_root_gripspace_metadata(&root, &linked_worktrees)?;
    create_root_gripspace_manifest(&root, &repo_name, &main_revision)?;
    for wt in &linked_worktrees {
        create_linked_griptree_metadata(&root, wt, &repo_name)?;
    }

    if json {
        let result = serde_json::json!({
            "root": root.display().to_string(),
            "repo_name": repo_name,
            "main_child": root.join(&repo_name).display().to_string(),
            "linked_worktrees": linked_worktrees.iter().map(|wt| serde_json::json!({
                "root": wt.root.display().to_string(),
                "child": wt.root.join(&repo_name).display().to_string(),
                "branch": wt.branch,
            })).collect::<Vec<_>>(),
            "worktree_repair": true,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        Output::success("Migration complete!");
        println!();
        println!("  Main repo moved to: {}/{}/", root.display(), repo_name);
        for wt in &linked_worktrees {
            println!(
                "  Linked worktree:   {}/{} ({})",
                wt.root.display(),
                repo_name,
                wt.branch.as_deref().unwrap_or("detached")
            );
        }
        if root.join(".synapt").exists() {
            println!("  .synapt/           stays at gripspace root");
        }
        if root.join(".claude").exists() {
            println!("  .claude/           stays at gripspace root");
        }
        if root.join(".env").exists() {
            println!("  .env               stays at gripspace root");
        }
        println!("  .gitgrip/          created (gripspace marker)");
        println!();
        Output::info("Next steps:");
        println!("  cd {}", root.display());
        println!("  gr tree list    # verify linked worktrees became griptrees");
        println!("  gr status       # verify repos are visible");
    }

    Ok(())
}

fn migration_keep_names() -> HashSet<&'static str> {
    [".synapt", ".claude", ".env", "_migrate_tmp"]
        .iter()
        .copied()
        .collect()
}

fn collect_paths_to_move(root: &Path, repo_name: &str) -> anyhow::Result<Vec<PathBuf>> {
    let keep = migration_keep_names();
    let mut to_move: Vec<PathBuf> = Vec::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy().to_string();
        if keep.contains(name_str.as_str()) || name_str == repo_name {
            continue;
        }
        to_move.push(entry.path());
    }
    Ok(to_move)
}

fn list_existing_worktrees(root: &Path) -> anyhow::Result<Vec<ExistingWorktree>> {
    let output = std::process::Command::new("git")
        .args(["worktree", "list", "--porcelain"])
        .current_dir(root)
        .output()
        .map_err(|e| anyhow::anyhow!("Failed to run git worktree list --porcelain: {}", e))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("git worktree list --porcelain failed: {}", stderr.trim());
    }
    parse_worktree_list_porcelain(&String::from_utf8_lossy(&output.stdout), root)
}

fn parse_worktree_list_porcelain(
    stdout: &str,
    main_root: &Path,
) -> anyhow::Result<Vec<ExistingWorktree>> {
    let mut worktrees = Vec::new();
    let mut current_path: Option<PathBuf> = None;
    let mut current_branch: Option<String> = None;

    let mut flush_current =
        |path: &mut Option<PathBuf>, branch: &mut Option<String>| -> anyhow::Result<()> {
            if let Some(root) = path.take() {
                let canonical = root.canonicalize().unwrap_or(root.clone());
                worktrees.push(ExistingWorktree {
                    is_main: canonical == main_root,
                    root,
                    branch: branch.take(),
                });
            }
            Ok(())
        };

    for line in stdout.lines() {
        if line.trim().is_empty() {
            flush_current(&mut current_path, &mut current_branch)?;
            continue;
        }
        if let Some(path) = line.strip_prefix("worktree ") {
            flush_current(&mut current_path, &mut current_branch)?;
            current_path = Some(PathBuf::from(path.trim()));
            continue;
        }
        if let Some(branch) = line.strip_prefix("branch ") {
            current_branch = Some(normalize_branch_name(branch.trim()));
        }
    }

    flush_current(&mut current_path, &mut current_branch)?;

    if !worktrees.iter().any(|wt| wt.is_main) {
        anyhow::bail!(
            "git worktree list did not include the main repo path {}",
            main_root.display()
        );
    }

    Ok(worktrees)
}

fn normalize_branch_name(branch: &str) -> String {
    branch
        .strip_prefix("refs/heads/")
        .unwrap_or(branch)
        .to_string()
}

fn validate_migration_targets(
    worktrees: &[ExistingWorktree],
    repo_name: &str,
) -> anyhow::Result<()> {
    for wt in worktrees {
        if !wt.is_main && wt.branch.is_none() {
            anyhow::bail!(
                "Linked worktree at {} is detached; migrate in-place requires branch-backed worktrees",
                wt.root.display()
            );
        }
        let child = wt.root.join(repo_name);
        if child.exists() {
            anyhow::bail!(
                "Child directory already exists: {}. Migration may have already run.",
                child.display()
            );
        }
    }
    Ok(())
}

fn migrate_single_worktree_root(root: &Path, repo_name: &str) -> anyhow::Result<()> {
    let to_move = collect_paths_to_move(root, repo_name)?;
    let tmp = root.join("_migrate_tmp");
    std::fs::create_dir_all(&tmp)?;

    for src in &to_move {
        let dest = tmp.join(src.file_name().unwrap());
        std::fs::rename(src, dest)
            .map_err(|e| anyhow::anyhow!("Failed to move {}: {}", src.display(), e))?;
    }

    let child = root.join(repo_name);
    std::fs::rename(&tmp, &child).map_err(|e| {
        anyhow::anyhow!(
            "Failed to rename _migrate_tmp to {}: {}",
            child.display(),
            e
        )
    })?;
    Ok(())
}

fn run_worktree_repairs(
    worktrees: &[ExistingWorktree],
    repo_name: &str,
) -> anyhow::Result<Vec<ProcessOutput>> {
    let main = worktrees
        .iter()
        .find(|wt| wt.is_main)
        .ok_or_else(|| anyhow::anyhow!("Missing main worktree entry"))?;
    let main_child = main.root.join(repo_name);
    let mut cmd = std::process::Command::new("git");
    cmd.args(["worktree", "repair"]).current_dir(&main_child);
    for wt in worktrees.iter().filter(|wt| !wt.is_main) {
        cmd.arg(wt.root.join(repo_name));
    }

    let output = cmd.output().map_err(|e| {
        anyhow::anyhow!(
            "Failed to run git worktree repair in {}: {}",
            main_child.display(),
            e
        )
    })?;

    Ok(vec![output])
}

fn create_root_gripspace_metadata(
    root: &Path,
    linked_worktrees: &[ExistingWorktree],
) -> anyhow::Result<()> {
    let gitgrip_dir = root.join(".gitgrip");
    std::fs::create_dir_all(&gitgrip_dir)?;
    let stale_child_marker = gitgrip_dir.join("griptree.json");
    if stale_child_marker.exists() {
        let _ = std::fs::remove_file(&stale_child_marker);
    }

    let mut griptrees = MigratedGriptreesList::default();
    for wt in linked_worktrees {
        let branch = wt.branch.as_ref().ok_or_else(|| {
            anyhow::anyhow!("Missing branch for linked worktree {}", wt.root.display())
        })?;
        griptrees.griptrees.insert(
            branch.clone(),
            MigratedGriptreeEntry {
                path: wt.root.to_string_lossy().to_string(),
                branch: branch.clone(),
                locked: false,
                lock_reason: None,
            },
        );
    }

    let griptrees_json = serde_json::to_string_pretty(&griptrees)?;
    std::fs::write(gitgrip_dir.join("griptrees.json"), griptrees_json)?;
    Ok(())
}

fn create_root_gripspace_manifest(
    root: &Path,
    repo_name: &str,
    revision: &str,
) -> anyhow::Result<()> {
    let repo_child = root.join(repo_name);
    let remote_url = std::process::Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(&repo_child)
        .output()
        .ok()
        .filter(|out| out.status.success())
        .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string())
        .filter(|s| !s.is_empty());

    let manifest_dir = manifest_paths::main_space_dir(root);
    std::fs::create_dir_all(&manifest_dir)?;

    let repo = RepoConfig {
        url: remote_url,
        remote: None,
        path: format!("./{}", repo_name),
        revision: Some(revision.to_string()),
        target: None,
        sync_remote: None,
        push_remote: None,
        copyfile: None,
        linkfile: None,
        platform: None,
        reference: false,
        groups: vec![],
        agent: None,
        clone_strategy: None,
    };

    let manifest = Manifest {
        version: 2,
        remotes: None,
        gripspaces: None,
        manifest: None,
        repos: HashMap::from([(repo_name.to_string(), repo)]),
        settings: ManifestSettings {
            pr_prefix: "[cross-repo]".to_string(),
            merge_strategy: MergeStrategy::default(),
            revision: Some(revision.to_string()),
            target: None,
            sync_remote: None,
            push_remote: None,
            clone_strategy: CloneStrategy::default(),
        },
        workspace: None,
    };

    let yaml = format!(
        "# Generated by gr migrate in-place\n{}",
        serde_yaml::to_string(&manifest)?
    );
    std::fs::write(manifest_dir.join("gripspace.yml"), yaml)?;
    Ok(())
}

fn create_linked_griptree_metadata(
    main_root: &Path,
    worktree: &ExistingWorktree,
    repo_name: &str,
) -> anyhow::Result<()> {
    let branch = worktree.branch.as_ref().ok_or_else(|| {
        anyhow::anyhow!(
            "Missing branch for linked worktree {}",
            worktree.root.display()
        )
    })?;
    let gitgrip_dir = worktree.root.join(".gitgrip");
    std::fs::create_dir_all(&gitgrip_dir)?;
    std::fs::write(gitgrip_dir.join("state.json"), "{}")?;

    let mut config = GriptreeConfig::new(branch, &worktree.root.to_string_lossy());
    config
        .repo_upstreams
        .insert(repo_name.to_string(), format!("origin/{}", branch));
    config.save(&gitgrip_dir.join("griptree.json"))?;

    let child = worktree.root.join(repo_name);
    let main_child = main_root.join(repo_name);
    let pointer = GriptreePointer {
        main_workspace: main_root.to_string_lossy().to_string(),
        branch: branch.clone(),
        locked: false,
        created_at: Some(Utc::now()),
        repos: vec![GriptreeRepoInfo {
            name: repo_name.to_string(),
            original_branch: branch.clone(),
            is_reference: false,
            worktree_name: None,
            worktree_path: Some(child.to_string_lossy().to_string()),
            main_repo_path: Some(main_child.to_string_lossy().to_string()),
        }],
        manifest_branch: None,
        manifest_worktree_name: None,
    };
    let pointer_json = serde_json::to_string_pretty(&pointer)?;
    std::fs::write(worktree.root.join(".griptree"), pointer_json)?;
    Ok(())
}

fn worktree_repair_must_succeed(repair: &ProcessOutput) -> anyhow::Result<()> {
    if repair.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&repair.stderr);
    anyhow::bail!("git worktree repair exited non-zero: {}", stderr.trim());
}

/// Derive the repo name from `git remote get-url origin`, falling back to dir name.
/// "git@github.com:GetConversa/conversa-app.git" → "conversa-app"
fn derive_repo_name_from_remote(repo: &Path) -> String {
    let result = std::process::Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(repo)
        .output();

    if let Ok(out) = result {
        if out.status.success() {
            let url = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !url.is_empty() {
                // SSH: git@github.com:org/repo.git  → after last '/' or ':'
                // HTTPS: https://github.com/org/repo.git → after last '/'
                let base = url.rsplit(['/', ':']).next().unwrap_or(&url).to_string();
                // Strip .git suffix
                return base.strip_suffix(".git").unwrap_or(&base).to_string();
            }
        }
    }

    // Fallback: use the directory name
    repo.file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string()
}

/// Check that git >= 2.30 is available (required for `git worktree repair`).
fn check_git_version_for_repair() -> anyhow::Result<()> {
    let out = std::process::Command::new("git")
        .arg("--version")
        .output()
        .map_err(|_| anyhow::anyhow!("git not found — is git installed?"))?;

    let version_str = String::from_utf8_lossy(&out.stdout);
    // "git version 2.43.0 (Apple Git-115)" → parse major.minor
    let parts: Vec<u32> = version_str
        .split_whitespace()
        .find(|s| s.contains('.'))
        .unwrap_or("0.0")
        .split('.')
        .take(2)
        .filter_map(|s| s.parse().ok())
        .collect();

    let (major, minor) = (
        parts.first().copied().unwrap_or(0),
        parts.get(1).copied().unwrap_or(0),
    );
    if major < 2 || (major == 2 && minor < 30) {
        anyhow::bail!(
            "git 2.30+ required for `git worktree repair` (found git {}.{}). \
             Please upgrade git.",
            major,
            minor
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn run_git(dir: &Path, args: &[&str]) -> ProcessOutput {
        std::process::Command::new("git")
            .args(args)
            .current_dir(dir)
            .output()
            .unwrap_or_else(|e| panic!("failed to run git {:?}: {}", args, e))
    }

    fn assert_git_ok(dir: &Path, args: &[&str]) -> ProcessOutput {
        let output = run_git(dir, args);
        assert!(
            output.status.success(),
            "git {:?} failed\nstdout:\n{}\nstderr:\n{}",
            args,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        output
    }

    fn normalize_git_path(path: &Path) -> String {
        path.canonicalize()
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/")
            .replace("//?/", "")
    }

    #[test]
    fn test_generate_manifest_yaml() {
        let repos = vec![
            ("GetConversa".to_string(), "conversa-app".to_string()),
            ("GetConversa".to_string(), "blessed-sound".to_string()),
        ];
        let yaml = generate_manifest_yaml(&repos, "synapt-dev", "consult-conversa");
        assert!(yaml.contains("version: 2"));
        assert!(yaml.contains("conversa-app:"));
        assert!(yaml.contains("blessed-sound:"));
        assert!(yaml.contains("GetConversa/conversa-app.git"));
        assert!(yaml.contains("consult-conversa-config:"));
        assert!(yaml.contains("clone_strategy: clone"));
    }

    #[test]
    fn test_generate_claude_md() {
        let repos = vec![("org".to_string(), "myrepo".to_string())];
        let agents = vec![AgentSpec {
            name: "atlas".to_string(),
            role: "research".to_string(),
            model: "claude-opus-4-6".to_string(),
            tool: "claude".to_string(),
        }];
        let md = generate_claude_md(&repos, "myproject", &agents, true);
        assert!(md.contains("# myproject"));
        assert!(md.contains("| **myrepo**"));
        assert!(md.contains("| **atlas**"));
        assert!(md.contains("Premium Features"));
        assert!(md.contains("recall_identity"));
    }

    #[test]
    fn test_generate_agents_toml_with_agents() {
        let agents = vec![AgentSpec {
            name: "apollo".to_string(),
            role: "implementation".to_string(),
            model: "claude-sonnet-4-6".to_string(),
            tool: "claude".to_string(),
        }];
        let toml = generate_agents_toml("myproject", &agents);
        assert!(toml.contains("[agents.apollo]"));
        assert!(toml.contains("role = \"implementation\""));
        assert!(toml.contains("model = \"claude-sonnet-4-6\""));
        assert!(toml.contains("config/prompts/apollo.md"));
    }

    #[test]
    fn test_generate_agents_toml_empty() {
        let toml = generate_agents_toml("myproject", &[]);
        assert!(toml.contains("session_name = \"myproject\""));
        assert!(toml.contains("# Add agents here:"));
    }

    #[test]
    fn test_migration_keep_names_includes_root_env() {
        let keep = migration_keep_names();
        assert!(keep.contains(".synapt"));
        assert!(keep.contains(".claude"));
        assert!(keep.contains(".env"));
        assert!(keep.contains("_migrate_tmp"));
    }

    #[test]
    fn test_worktree_repair_failure_is_error() {
        let output = std::process::Command::new("git")
            .args(["worktree", "definitely-not-a-subcommand"])
            .output()
            .expect("git should be runnable in test environment");
        assert!(worktree_repair_must_succeed(&output).is_err());
    }

    #[test]
    fn test_parse_worktree_list_marks_main_and_branch_names() {
        let main = PathBuf::from("/tmp/conversa")
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from("/tmp/conversa"));
        let porcelain = "\
worktree /tmp/conversa
HEAD deadbeef
branch refs/heads/main

worktree /tmp/conversa-dev
HEAD feedface
branch refs/heads/feat/dev
";

        let worktrees = parse_worktree_list_porcelain(porcelain, &main).unwrap();
        assert_eq!(worktrees.len(), 2);
        assert!(worktrees[0].is_main);
        assert_eq!(worktrees[0].branch.as_deref(), Some("main"));
        assert!(!worktrees[1].is_main);
        assert_eq!(worktrees[1].branch.as_deref(), Some("feat/dev"));
    }

    #[tokio::test]
    async fn test_migrate_in_place_converts_linked_worktrees_into_griptrees() {
        let temp = TempDir::new().unwrap();
        let main_root = temp.path().join("conversa");
        let linked_root = temp.path().join("conversa-dev");
        std::fs::create_dir_all(&main_root).unwrap();

        assert_git_ok(&main_root, &["init", "-b", "main"]);
        assert_git_ok(&main_root, &["config", "user.name", "Test User"]);
        assert_git_ok(&main_root, &["config", "user.email", "test@example.com"]);
        std::fs::write(main_root.join("README.md"), "hello\n").unwrap();
        std::fs::write(main_root.join(".env"), "FOO=bar\n").unwrap();
        assert_git_ok(&main_root, &["add", "."]);
        assert_git_ok(&main_root, &["commit", "-m", "init"]);
        assert_git_ok(
            &main_root,
            &[
                "remote",
                "add",
                "origin",
                "git@github.com:GetConversa/conversa-app.git",
            ],
        );

        assert_git_ok(
            &main_root,
            &[
                "worktree",
                "add",
                linked_root.to_str().unwrap(),
                "-b",
                "feat/dev",
            ],
        );
        std::fs::create_dir_all(linked_root.join(".claude")).unwrap();

        run_migrate_in_place(Some(main_root.to_str().unwrap()), false, false)
            .await
            .unwrap();

        let repo_name = "conversa-app";
        let main_child = main_root.join(repo_name);
        let linked_child = linked_root.join(repo_name);

        assert!(main_child.exists(), "main child repo should exist");
        assert!(linked_child.exists(), "linked child repo should exist");
        assert!(
            main_root.join(".env").exists(),
            ".env should stay at main root"
        );
        assert!(
            linked_root.join(".claude").exists(),
            ".claude should stay at linked worktree root"
        );

        let griptrees: MigratedGriptreesList = serde_json::from_str(
            &std::fs::read_to_string(main_root.join(".gitgrip").join("griptrees.json")).unwrap(),
        )
        .unwrap();
        let entry = griptrees.griptrees.get("feat/dev").unwrap();
        assert_eq!(
            PathBuf::from(&entry.path).canonicalize().unwrap(),
            linked_root.canonicalize().unwrap()
        );

        let griptree_cfg = GriptreeConfig::load_from_workspace(&linked_root)
            .unwrap()
            .unwrap();
        assert_eq!(griptree_cfg.branch, "feat/dev");

        let manifest_path = manifest_paths::main_space_dir(&main_root).join("gripspace.yml");
        assert!(
            manifest_path.exists(),
            "root manifest should exist after migration"
        );
        let manifest = crate::core::manifest::Manifest::load(&manifest_path).unwrap();
        let repo = manifest.repos.get(repo_name).unwrap();
        assert_eq!(repo.path, format!("./{}", repo_name));
        assert_eq!(repo.revision.as_deref(), Some("main"));
        assert_eq!(
            repo.url.as_deref(),
            Some("git@github.com:GetConversa/conversa-app.git")
        );

        let pointer = GriptreePointer::load(&linked_root.join(".griptree")).unwrap();
        assert_eq!(
            PathBuf::from(pointer.main_workspace)
                .canonicalize()
                .unwrap(),
            main_root.canonicalize().unwrap()
        );
        assert_eq!(pointer.branch, "feat/dev");
        assert_eq!(pointer.repos.len(), 1);
        assert_eq!(pointer.repos[0].name, repo_name);
        assert_eq!(
            PathBuf::from(pointer.repos[0].worktree_path.as_ref().unwrap())
                .canonicalize()
                .unwrap(),
            linked_child.canonicalize().unwrap()
        );
        assert_eq!(
            PathBuf::from(pointer.repos[0].main_repo_path.as_ref().unwrap())
                .canonicalize()
                .unwrap(),
            main_child.canonicalize().unwrap()
        );

        assert_git_ok(&main_child, &["status", "--short"]);
        let linked_branch = assert_git_ok(&linked_child, &["rev-parse", "--abbrev-ref", "HEAD"]);
        assert_eq!(
            String::from_utf8_lossy(&linked_branch.stdout).trim(),
            "feat/dev"
        );

        let worktree_list = assert_git_ok(&main_child, &["worktree", "list", "--porcelain"]);
        let worktree_stdout = String::from_utf8_lossy(&worktree_list.stdout).replace('\\', "/");
        assert!(worktree_stdout.contains(&normalize_git_path(&linked_child)));
    }
}
