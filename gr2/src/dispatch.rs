use anyhow::Result;
use std::fs;
use std::path::PathBuf;

use crate::args::{Commands, RepoCommands, TeamCommands};

pub async fn dispatch_command(command: Commands, verbose: bool) -> Result<()> {
    match command {
        Commands::Init { path, name } => {
            let workspace_root = PathBuf::from(path);
            let workspace_name = name.unwrap_or_else(|| {
                workspace_root
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "workspace".to_string())
            });

            if workspace_root.exists() {
                anyhow::bail!(
                    "workspace path already exists: {}",
                    workspace_root.display()
                );
            }

            fs::create_dir_all(workspace_root.join(".grip"))?;
            fs::create_dir_all(workspace_root.join("config"))?;
            fs::create_dir_all(workspace_root.join("agents"))?;
            fs::create_dir_all(workspace_root.join("repos"))?;

            let workspace_toml = format!(
                "version = 2\nname = \"{}\"\nlayout = \"team-workspace\"\n",
                workspace_name
            );
            fs::write(workspace_root.join(".grip/workspace.toml"), workspace_toml)?;

            println!(
                "Initialized gr2 team workspace '{}' at {}",
                workspace_name,
                workspace_root.display()
            );
            Ok(())
        }
        Commands::Doctor => {
            if verbose {
                println!("gr2 bootstrap OK (verbose)");
            } else {
                println!("gr2 bootstrap OK");
            }
            Ok(())
        }
        Commands::Team { command } => match command {
            TeamCommands::Add { name } => {
                let workspace_root = require_workspace_root()?;

                let agent_root = workspace_root.join("agents").join(&name);
                if agent_root.exists() {
                    anyhow::bail!("agent '{}' already exists", name);
                }

                fs::create_dir_all(&agent_root)?;
                fs::write(
                    agent_root.join("agent.toml"),
                    format!("name = \"{}\"\nkind = \"agent-workspace\"\n", name),
                )?;

                println!("Added gr2 agent workspace '{}'", name);
                Ok(())
            }
            TeamCommands::List => {
                let workspace_root = require_workspace_root()?;
                let agents_root = workspace_root.join("agents");

                let mut names = Vec::new();
                for entry in fs::read_dir(&agents_root)? {
                    let entry = entry?;
                    if entry.file_type()?.is_dir() && entry.path().join("agent.toml").exists() {
                        names.push(entry.file_name().to_string_lossy().into_owned());
                    }
                }

                names.sort();

                if names.is_empty() {
                    println!("No gr2 agent workspaces registered.");
                } else {
                    println!("Agent workspaces");
                    for name in names {
                        println!("- {}", name);
                    }
                }

                Ok(())
            }
            TeamCommands::Remove { name } => {
                let workspace_root = require_workspace_root()?;
                let agent_root = workspace_root.join("agents").join(&name);

                if !agent_root.join("agent.toml").exists() {
                    anyhow::bail!("agent '{}' not found", name);
                }

                fs::remove_dir_all(&agent_root)?;
                println!("Removed gr2 agent workspace '{}'", name);
                Ok(())
            }
        },
        Commands::Repo { command } => match command {
            RepoCommands::Add { name, url } => {
                let workspace_root = require_workspace_root()?;
                let repos_root = workspace_root.join("repos");
                let registry_path = workspace_root.join(".grip/repos.toml");
                let repo_dir = repos_root.join(&name);

                if repo_dir.exists() {
                    anyhow::bail!("repo '{}' already exists", name);
                }

                fs::create_dir_all(&repo_dir)?;
                fs::write(
                    repo_dir.join("repo.toml"),
                    format!("name = \"{}\"\nurl = \"{}\"\n", name, url),
                )?;

                let mut entries = Vec::new();
                if registry_path.exists() {
                    entries.push(fs::read_to_string(&registry_path)?);
                }
                entries.push(format!(
                    "[[repo]]\nname = \"{}\"\nurl = \"{}\"\n",
                    name, url
                ));
                fs::write(&registry_path, entries.join("\n"))?;

                println!("Added gr2 repo '{}' -> {}", name, url);
                Ok(())
            }
        },
    }
}

fn require_workspace_root() -> Result<PathBuf> {
    let workspace_root = std::env::current_dir()?;
    let workspace_toml = workspace_root.join(".grip/workspace.toml");
    if !workspace_toml.exists() {
        anyhow::bail!("not in a gr2 workspace: missing .grip/workspace.toml");
    }
    Ok(workspace_root)
}
