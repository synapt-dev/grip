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
    _create_repos: bool,
    _private: bool,
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
    yaml.push_str("  revision: main\n");
    yaml.push_str("  linkfile:\n");
    yaml.push_str("    - src: config/CLAUDE.md\n");
    yaml.push_str("      dest: CLAUDE.md\n");
    yaml.push_str("    - src: config/agents.toml\n");
    yaml.push_str("      dest: .gitgrip/agents.toml\n\n");

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

    // Config repo
    yaml.push_str(&format!("  {}-config:\n", prefix));
    yaml.push_str(&format!(
        "    url: https://github.com/{}/{}-config.git\n",
        org, prefix
    ));
    yaml.push_str("    path: ./config\n");
    yaml.push_str("    revision: main\n\n");

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

    // Initialize manifest as git repo
    let _ = std::process::Command::new("git")
        .args(["init", "-b", "main"])
        .current_dir(&manifest_dir)
        .output();
    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(&manifest_dir)
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "Initial gripspace manifest"])
        .current_dir(&manifest_dir)
        .output();

    Ok(())
}
