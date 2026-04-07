//! `gr migrate` — convert existing repos into a gripspace (#424).
//!
//! First customer migration tooling. Generates gripspace.yml +
//! agents.toml + CLAUDE.md + per-agent prompts from a list of
//! GitHub repos. Interactive mode configures the full agent team.

use crate::cli::output::Output;
use dialoguer::{theme::ColorfulTheme, Confirm, Input, Select};
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

/// Agent configuration collected during interactive setup.
struct AgentSpec {
    name: String,
    role: String,
    model: String,
    tool: String,
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

#[cfg(test)]
mod tests {
    use super::*;

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
}

// ---------------------------------------------------------------------------
// gr migrate in-place (#456)
// ---------------------------------------------------------------------------

/// Migrate an existing git repo directory into a gripspace in-place.
///
/// Algorithm v3.1 from the design session:
/// 1. Derive repo name from git remote URL
/// 2. Move everything into a child dir named after the repo
/// 3. Copy metadata (.synapt, .claude, .env) back to gripspace root
/// 4. Run `git worktree repair` (requires git 2.30+)
/// 5. Initialize gripspace structure (.gitgrip/)
pub async fn run_migrate_in_place(
    path: Option<&str>,
    dry_run: bool,
    json: bool,
) -> anyhow::Result<()> {
    let target = match path {
        Some(p) => PathBuf::from(p),
        None => std::env::current_dir()?,
    };

    // Verify it's a git repo
    if !target.join(".git").exists() {
        anyhow::bail!(
            "{} is not a git repository (no .git found)",
            target.display()
        );
    }

    // Derive repo name from remote URL
    let repo_name = get_repo_name(&target)?;

    if !json {
        Output::header("Migrating repo to gripspace in-place...");
        println!();
        Output::info(&format!("Directory: {}", target.display()));
        Output::info(&format!("Repo name: {}", repo_name));
        Output::info(&format!("Result: {}/{}/", target.display(), repo_name));
        println!();
    }

    if dry_run {
        if !json {
            Output::info("Dry run — no changes made.");
            println!();
            println!("Would:");
            println!("  1. Move all files into ./{}/", repo_name);
            println!("  2. Copy .synapt/, .claude/, .env back to root");
            println!("  3. Run git worktree repair in ./{}/", repo_name);
            println!("  4. Create .gitgrip/ structure");
        }
        if json {
            let result = serde_json::json!({
                "dry_run": true,
                "repo_name": repo_name,
                "target": target.display().to_string(),
            });
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
        return Ok(());
    }

    // Check git version >= 2.30 (for worktree repair)
    check_git_version()?;

    let child_dir = target.join(&repo_name);
    if child_dir.exists() {
        anyhow::bail!(
            "Child directory {}/{} already exists. Already migrated?",
            target.display(),
            repo_name
        );
    }

    // Step 1: Move everything into a temp dir
    let tmp_name = "_tmp_migrate";
    let tmp_dir = target.join(tmp_name);
    std::fs::create_dir(&tmp_dir)?;

    // Metadata dirs that stay at root (will be copied back)
    let metadata_dirs = [".synapt", ".claude", ".gitgrip"];
    let metadata_files = [".env"];

    for entry in std::fs::read_dir(&target)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();

        // Skip the temp dir itself
        if name_str == tmp_name {
            continue;
        }

        // Move everything into temp
        let src = entry.path();
        let dst = tmp_dir.join(&name);
        if let Err(e) = std::fs::rename(&src, &dst) {
            Output::warning(&format!("Could not move {}: {}", name_str, e));
        }
    }

    // Step 2: Rename temp to repo name
    std::fs::rename(&tmp_dir, &child_dir)?;
    if !json {
        Output::success(&format!("Moved files into {}/", repo_name));
    }

    // Step 3: Copy metadata back to gripspace root
    for dir_name in &metadata_dirs {
        let src = child_dir.join(dir_name);
        if src.is_dir() {
            let dst = target.join(dir_name);
            copy_dir_recursive(&src, &dst)?;
            if !json {
                Output::info(&format!("Copied {}/ to gripspace root", dir_name));
            }
        }
    }
    for file_name in &metadata_files {
        let src = child_dir.join(file_name);
        if src.is_file() {
            let dst = target.join(file_name);
            std::fs::copy(&src, &dst)?;
            if !json {
                Output::info(&format!("Copied {} to gripspace root", file_name));
            }
        }
    }

    // Step 4: git worktree repair
    let repair = std::process::Command::new("git")
        .args(["worktree", "repair"])
        .current_dir(&child_dir)
        .output();

    match repair {
        Ok(output) if output.status.success() => {
            if !json {
                Output::success("git worktree repair succeeded");
            }
        }
        Ok(output) => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            Output::warning(&format!("git worktree repair: {}", stderr.trim()));
        }
        Err(e) => {
            Output::warning(&format!("git worktree repair failed: {}", e));
        }
    }

    // Step 5: Create .gitgrip structure
    let gitgrip_dir = target.join(".gitgrip");
    std::fs::create_dir_all(&gitgrip_dir)?;

    if !json {
        println!();
        Output::success("Migration complete!");
        println!();
        println!("  {}/", target.display());
        println!("    .gitgrip/          (gripspace marker)");
        println!("    .synapt/           (recall data)");
        println!("    .claude/           (Claude Code settings)");
        println!("    {}/          (repo)", repo_name);
        println!();
        println!("Next steps:");
        println!("  1. Create gripspace manifest (gripspace.yml)");
        println!("  2. gr init --in-place");
        println!("  3. gr sync && gr status");
    }

    if json {
        let result = serde_json::json!({
            "success": true,
            "repo_name": repo_name,
            "child_dir": child_dir.display().to_string(),
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

/// Derive repo name from git remote URL, fallback to directory name.
fn get_repo_name(repo_dir: &Path) -> anyhow::Result<String> {
    let output = std::process::Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(repo_dir)
        .output();

    if let Ok(output) = output {
        if output.status.success() {
            let url = String::from_utf8_lossy(&output.stdout).trim().to_string();
            // Extract name from URL: git@github.com:org/name.git → name
            let name = url
                .rsplit('/')
                .next()
                .unwrap_or(&url)
                .trim_end_matches(".git")
                .to_string();
            if !name.is_empty() {
                return Ok(name);
            }
        }
    }

    // Fallback: directory name
    repo_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .ok_or_else(|| anyhow::anyhow!("Cannot determine repo name"))
}

/// Check that git version is >= 2.30 (required for worktree repair).
fn check_git_version() -> anyhow::Result<()> {
    let output = std::process::Command::new("git")
        .args(["--version"])
        .output()?;
    let version_str = String::from_utf8_lossy(&output.stdout);
    // Parse "git version 2.XX.Y"
    if let Some(ver) = version_str.split_whitespace().nth(2) {
        let parts: Vec<u32> = ver.split('.').filter_map(|p| p.parse().ok()).collect();
        if parts.len() >= 2 && (parts[0] > 2 || (parts[0] == 2 && parts[1] >= 30)) {
            return Ok(());
        }
        anyhow::bail!(
            "git {} is too old. git 2.30+ required for worktree repair.",
            ver
        );
    }
    Ok(()) // Can't parse, proceed anyway
}

/// Recursively copy a directory.
fn copy_dir_recursive(src: &Path, dst: &Path) -> anyhow::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());
        if src_path.is_dir() {
            copy_dir_recursive(&src_path, &dst_path)?;
        } else {
            std::fs::copy(&src_path, &dst_path)?;
        }
    }
    Ok(())
}
