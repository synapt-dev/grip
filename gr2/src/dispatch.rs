use anyhow::Result;
use std::fs;
use std::path::PathBuf;

use crate::args::Commands;

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
    }
}
