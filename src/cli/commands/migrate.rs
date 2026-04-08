//! `gr migrate` — convert existing repos into a gripspace (#424).
//!
//! Two subcommands:
//!   - `from-repos`: Generate a new gripspace from GitHub repo list
//!   - `in-place`:   Convert an existing git repo dir into a gripspace

use crate::cli::output::Output;
use dialoguer::{theme::ColorfulTheme, Confirm, Input, Select};
use std::collections::HashSet;
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
/// Algorithm (v3.1):
/// 1. Derive repo name from `git remote get-url origin` (basename, strip .git)
///    Falls back to directory name if no remote or command fails.
/// 2. Create `_migrate_tmp/` and move everything into it EXCEPT:
///    .synapt/, .claude/, .env, _migrate_tmp/, and the child dir name
/// 3. Rename `_migrate_tmp/` → `<repo-name>/`
/// 4. Run `git worktree repair` inside `<repo-name>/` to fix linked worktree paths
/// 5. Create `.gitgrip/` marker at the root to signal this is a gripspace
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
    let child = root.join(&repo_name);

    if !json {
        Output::header("gr migrate in-place");
        println!();
        Output::info(&format!("Gripspace root: {}", root.display()));
        Output::info(&format!("Repo child:     {}/{}", root.display(), repo_name));
        println!();
        if dry_run {
            Output::warning("DRY RUN — no changes will be made");
            println!();
        }
    }

    if child.exists() {
        anyhow::bail!(
            "Child directory already exists: {}. \
             Migration may have already run, or choose a different path.",
            child.display()
        );
    }

    // Enumerate what will move vs stay
    let keep = migration_keep_names();

    let mut to_move: Vec<PathBuf> = Vec::new();
    for entry in std::fs::read_dir(&root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy().to_string();
        if keep.contains(name_str.as_str()) || name_str == repo_name {
            continue;
        }
        to_move.push(entry.path());
    }

    if !json && dry_run {
        println!("  Would move to {}/{}:", root.display(), repo_name);
        for p in &to_move {
            println!(
                "    {}",
                p.file_name().unwrap_or_default().to_string_lossy()
            );
        }
        println!();
        println!("  Would keep at gripspace root:");
        for path in [".synapt", ".claude", ".env"] {
            if root.join(path).exists() {
                if root.join(path).is_dir() {
                    println!("    {}/", path);
                } else {
                    println!("    {}", path);
                }
            }
        }
        // Show linked worktrees that will be repaired
        let wt_out = std::process::Command::new("git")
            .args(["worktree", "list", "--porcelain"])
            .current_dir(&root)
            .output();
        let linked_worktrees: Vec<String> = if let Ok(out) = wt_out {
            String::from_utf8_lossy(&out.stdout)
                .lines()
                .filter_map(|l| l.strip_prefix("worktree "))
                .filter(|p| *p != root.to_string_lossy().as_ref())
                .map(|p| p.to_string())
                .collect()
        } else {
            Vec::new()
        };

        println!();
        if linked_worktrees.is_empty() {
            println!("  Would run: git worktree repair (in {}/)", repo_name);
        } else {
            println!(
                "  Would repair {} linked worktree(s):",
                linked_worktrees.len()
            );
            for wt in &linked_worktrees {
                println!("    {}", wt);
            }
        }
        println!("  Would create: .gitgrip/");
        return Ok(());
    }

    if dry_run {
        let result = serde_json::json!({
            "root": root.display().to_string(),
            "repo_name": repo_name,
            "to_move": to_move.iter()
                .map(|p| p.file_name().unwrap_or_default().to_string_lossy().to_string())
                .collect::<Vec<_>>(),
            "dry_run": true,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    // Step 1: create temp dir
    let tmp = root.join("_migrate_tmp");
    std::fs::create_dir_all(&tmp)?;

    // Step 2: move everything (except excluded) into temp
    for src in &to_move {
        let dest = tmp.join(src.file_name().unwrap());
        std::fs::rename(src, dest)
            .map_err(|e| anyhow::anyhow!("Failed to move {}: {}", src.display(), e))?;
    }

    // Step 3: rename temp → child
    std::fs::rename(&tmp, &child).map_err(|e| {
        anyhow::anyhow!(
            "Failed to rename _migrate_tmp to {}: {}",
            child.display(),
            e
        )
    })?;

    // Step 4: git worktree repair (fixes linked worktree .git file paths)
    let repair = std::process::Command::new("git")
        .args(["worktree", "repair"])
        .current_dir(&child)
        .output()
        .map_err(|e| anyhow::anyhow!("Failed to run git worktree repair: {}", e))?;

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

    worktree_repair_must_succeed(&repair)?;

    // Step 5: create .gitgrip/ marker
    std::fs::create_dir_all(root.join(".gitgrip"))?;

    if json {
        let result = serde_json::json!({
            "root": root.display().to_string(),
            "repo_name": repo_name,
            "child": child.display().to_string(),
            "worktree_repair": repair.status.success(),
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        Output::success("Migration complete!");
        println!();
        println!("  Repo moved to:  {}/{}/", root.display(), repo_name);
        if root.join(".synapt").exists() {
            println!("  .synapt/        stays at gripspace root");
        }
        if root.join(".claude").exists() {
            println!("  .claude/        stays at gripspace root");
        }
        if root.join(".env").exists() {
            println!("  .env            stays at gripspace root");
        }
        println!("  .gitgrip/       created (gripspace marker)");
        println!();
        Output::info("Next steps:");
        println!("  cd {}", root.display());
        println!("  gr status       # verify repos are visible");
        println!("  gr spawn up     # launch agents (once gripspace.yml is configured)");
    }

    Ok(())
}

fn migration_keep_names() -> HashSet<&'static str> {
    [".synapt", ".claude", ".env", "_migrate_tmp"]
        .iter()
        .copied()
        .collect()
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
}
