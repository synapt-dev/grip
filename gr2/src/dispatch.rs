use anyhow::Result;
use std::fs;
use std::path::PathBuf;

use crate::args::{Commands, RepoCommands, SpecCommands, TeamCommands, UnitCommands};
use crate::plan::ExecutionPlan;
use crate::repo_status::{RepoStatusFilter, RepoStatusReport};
use crate::spec::{read_workspace_spec, workspace_spec_path, write_workspace_spec, WorkspaceSpec};

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
            RepoCommands::List => {
                let workspace_root = require_workspace_root()?;
                let repos_root = workspace_root.join("repos");

                let mut repos = Vec::new();
                for entry in fs::read_dir(&repos_root)? {
                    let entry = entry?;
                    let repo_toml = entry.path().join("repo.toml");
                    if entry.file_type()?.is_dir() && repo_toml.exists() {
                        let content = fs::read_to_string(repo_toml)?;
                        let fallback_name = entry.file_name().to_string_lossy().into_owned();
                        let name = content
                            .lines()
                            .find_map(|line| line.strip_prefix("name = \""))
                            .and_then(|line| line.strip_suffix('"'))
                            .map(str::to_owned)
                            .unwrap_or(fallback_name);
                        let url = content
                            .lines()
                            .find_map(|line| line.strip_prefix("url = \""))
                            .and_then(|line| line.strip_suffix('"'))
                            .unwrap_or("")
                            .to_string();
                        repos.push((name, url));
                    }
                }

                repos.sort_by(|a, b| a.0.cmp(&b.0));

                if repos.is_empty() {
                    println!("No gr2 repos registered.");
                } else {
                    println!("Repos");
                    for (name, url) in repos {
                        println!("- {} -> {}", name, url);
                    }
                }

                Ok(())
            }
            RepoCommands::Status { unit, repo } => {
                let workspace_root = require_workspace_root()?;
                let report =
                    RepoStatusReport::load(&workspace_root, &RepoStatusFilter { unit, repo })?;
                println!("{}", report.render_table());
                Ok(())
            }
            RepoCommands::Remove { name } => {
                let workspace_root = require_workspace_root()?;
                let repos_root = workspace_root.join("repos");
                let repo_root = repos_root.join(&name);
                let repo_toml = repo_root.join("repo.toml");

                if !repo_toml.exists() {
                    anyhow::bail!("repo '{}' not found", name);
                }

                fs::remove_dir_all(&repo_root)?;

                let registry_path = workspace_root.join(".grip/repos.toml");
                if registry_path.exists() {
                    let registry = fs::read_to_string(&registry_path)?;
                    let kept_entries = registry
                        .split("\n[[repo]]\n")
                        .filter_map(|chunk| {
                            let chunk = chunk.trim();
                            if chunk.is_empty() {
                                return None;
                            }
                            let normalized = if chunk.starts_with("[[repo]]") {
                                chunk.to_string()
                            } else {
                                format!("[[repo]]\n{}", chunk)
                            };
                            let matches_name = normalized
                                .lines()
                                .find_map(|line| line.strip_prefix("name = \""))
                                .and_then(|line| line.strip_suffix('"'))
                                .map(|entry_name| entry_name == name)
                                .unwrap_or(false);
                            if matches_name {
                                None
                            } else {
                                Some(normalized)
                            }
                        })
                        .collect::<Vec<_>>();

                    if kept_entries.is_empty() {
                        fs::remove_file(&registry_path)?;
                    } else {
                        fs::write(&registry_path, kept_entries.join("\n\n"))?;
                    }
                }

                println!("Removed gr2 repo '{}'", name);
                Ok(())
            }
        },
        Commands::Unit { command } => match command {
            UnitCommands::Add { name } => {
                let workspace_root = require_workspace_root()?;
                validate_unit_name(&name)?;
                let units_root = workspace_root.join("agents");
                let registry_path = workspace_root.join(".grip/units.toml");
                let unit_root = units_root.join(&name);

                if unit_root.exists() {
                    anyhow::bail!("unit '{}' already exists", name);
                }

                fs::create_dir_all(&unit_root)?;
                fs::write(
                    unit_root.join("unit.toml"),
                    format!("name = \"{}\"\nkind = \"unit\"\n", name),
                )?;

                let mut entries = Vec::new();
                if registry_path.exists() {
                    entries.push(fs::read_to_string(&registry_path)?);
                }
                entries.push(format!("[[unit]]\nname = \"{}\"\nkind = \"unit\"\n", name));
                fs::write(&registry_path, entries.join("\n"))?;

                println!("Added gr2 unit '{}'", name);
                Ok(())
            }
            UnitCommands::List => {
                let workspace_root = require_workspace_root()?;
                let units_root = workspace_root.join("agents");

                let mut names = Vec::new();
                for entry in fs::read_dir(&units_root)? {
                    let entry = entry?;
                    if entry.file_type()?.is_dir() && entry.path().join("unit.toml").exists() {
                        names.push(entry.file_name().to_string_lossy().into_owned());
                    }
                }

                names.sort();

                if names.is_empty() {
                    println!("No gr2 units registered.");
                } else {
                    println!("Units");
                    for name in names {
                        println!("- {}", name);
                    }
                }

                Ok(())
            }
            UnitCommands::Remove { name } => {
                let workspace_root = require_workspace_root()?;
                let units_root = workspace_root.join("agents");
                let unit_root = units_root.join(&name);
                let unit_toml = unit_root.join("unit.toml");

                if !unit_toml.exists() {
                    anyhow::bail!("unit '{}' not found", name);
                }

                fs::remove_dir_all(&unit_root)?;

                let registry_path = workspace_root.join(".grip/units.toml");
                if registry_path.exists() {
                    let registry = fs::read_to_string(&registry_path)?;
                    let kept_entries = registry
                        .split("\n[[unit]]\n")
                        .filter_map(|chunk| {
                            let chunk = chunk.trim();
                            if chunk.is_empty() {
                                return None;
                            }
                            let normalized = if chunk.starts_with("[[unit]]") {
                                chunk.to_string()
                            } else {
                                format!("[[unit]]\n{}", chunk)
                            };
                            let matches_name = normalized
                                .lines()
                                .find_map(|line| line.strip_prefix("name = \""))
                                .and_then(|line| line.strip_suffix('"'))
                                .map(|entry_name| entry_name == name)
                                .unwrap_or(false);
                            if matches_name {
                                None
                            } else {
                                Some(normalized)
                            }
                        })
                        .collect::<Vec<_>>();

                    if kept_entries.is_empty() {
                        fs::remove_file(&registry_path)?;
                    } else {
                        fs::write(&registry_path, kept_entries.join("\n\n"))?;
                    }
                }

                println!("Removed gr2 unit '{}'", name);
                Ok(())
            }
        },
        Commands::Spec { command } => match command {
            SpecCommands::Show => {
                let workspace_root = require_workspace_root()?;
                let spec_path = workspace_spec_path(&workspace_root);
                let spec = if spec_path.exists() {
                    read_workspace_spec(&workspace_root)?
                } else {
                    let spec = WorkspaceSpec::from_workspace(&workspace_root)?;
                    write_workspace_spec(&workspace_root, &spec)?;
                    spec
                };

                println!("{}", toml::to_string_pretty(&spec)?);
                Ok(())
            }
            SpecCommands::Validate => {
                let workspace_root = require_workspace_root()?;
                let spec = read_workspace_spec(&workspace_root)?;
                spec.validate(&workspace_root)?;
                println!(
                    "Workspace spec is valid: {}",
                    workspace_spec_path(&workspace_root).display()
                );
                Ok(())
            }
        },
        Commands::Plan { yes } => {
            let workspace_root = require_workspace_root()?;
            let build = ExecutionPlan::from_workspace_spec(&workspace_root)?;
            let guard_report =
                build
                    .plan
                    .guard_for_apply(&workspace_root, &build.spec, yes, false)?;

            if build.generated_spec {
                println!(
                    "Generated workspace spec at {} from current workspace state.",
                    workspace_spec_path(&workspace_root).display()
                );
            }
            println!("{}", build.plan.render_table());
            for warning in guard_report.warnings {
                println!("warning: {}", warning);
            }
            if guard_report.requires_confirmation {
                println!("warning: plan contains more than 3 operations; apply will require --yes");
            }
            Ok(())
        }
        Commands::Apply { yes, autostash } => {
            let workspace_root = require_workspace_root()?;
            let build = ExecutionPlan::from_workspace_spec(&workspace_root)?;
            let guard_report =
                build
                    .plan
                    .guard_for_apply(&workspace_root, &build.spec, yes, autostash)?;

            if build.generated_spec {
                println!(
                    "Generated workspace spec at {} from current workspace state.",
                    workspace_spec_path(&workspace_root).display()
                );
            }

            if guard_report.requires_confirmation {
                anyhow::bail!("plan contains more than 3 operations; rerun with --yes to apply it");
            }

            if guard_report.has_dirty_repos && !autostash {
                anyhow::bail!(
                    "refusing to apply: units have repos with uncommitted changes; \
                     rerun with --autostash to stash and restore them automatically"
                );
            }

            for warning in &guard_report.warnings {
                println!("warning: {}", warning);
            }

            let applied =
                build
                    .plan
                    .apply(&workspace_root, &build.spec, &guard_report.dirty_repos)?;
            if applied.is_empty() {
                println!("ExecutionPlan");
                println!("- no changes required");
            } else {
                println!("Applied execution plan");
                for line in applied {
                    println!("- {}", line);
                }
            }
            Ok(())
        }
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

fn validate_unit_name(name: &str) -> Result<()> {
    if name.is_empty() {
        anyhow::bail!("unit name must not be empty");
    }

    if !name
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
    {
        anyhow::bail!(
            "invalid unit name '{}': use only ASCII letters, numbers, '_' or '-'",
            name
        );
    }

    Ok(())
}
