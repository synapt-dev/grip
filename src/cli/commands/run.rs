//! Run command implementation
//!
//! Runs workspace scripts defined in manifest.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use std::path::PathBuf;
use std::process::Command;

/// Run the run command
pub fn run_run(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    script_name: Option<&str>,
    list: bool,
) -> anyhow::Result<()> {
    let scripts = manifest.workspace.as_ref().and_then(|w| w.scripts.as_ref());

    if list || script_name.is_none() {
        // List available scripts
        Output::header("Workspace Scripts");
        println!();

        match scripts {
            Some(scripts) if !scripts.is_empty() => {
                for (name, script) in scripts {
                    let desc = script
                        .command
                        .as_deref()
                        .or_else(|| script.steps.as_ref().map(|_| "[multi-step]"))
                        .unwrap_or("[no command]");
                    println!("  {} - {}", name, desc);
                }
            }
            _ => {
                println!("  No scripts defined in manifest.");
                println!();
                println!("Define scripts in gripspace.yml:");
                println!("  workspace:");
                println!("    scripts:");
                println!("      build:");
                println!("        command: pnpm build");
            }
        }
        return Ok(());
    }

    let Some(name) = script_name else {
        return Ok(());
    };

    // Find the script
    let script = scripts.and_then(|s| s.get(name)).ok_or_else(|| {
        anyhow::anyhow!(
            "Script '{}' not found. Run `gr run --list` to see available scripts.",
            name
        )
    })?;

    Output::header(&format!("Running script: {}", name));
    println!();

    // Execute the script
    if let Some(ref command) = script.command {
        // Single command script
        run_command(workspace_root, command)?;
    } else if let Some(ref steps) = script.steps {
        // Multi-step script
        for (i, step) in steps.iter().enumerate() {
            println!(
                "Step {}/{}: {} - {}",
                i + 1,
                steps.len(),
                step.name,
                step.command
            );
            let working_dir = step
                .cwd
                .as_ref()
                .map(|p| workspace_root.join(p))
                .unwrap_or_else(|| workspace_root.clone());
            run_command(&working_dir, &step.command)?;
            println!();
        }
    } else {
        anyhow::bail!(
            "Script '{}' has no command or steps defined. \
             Check your gripspace.yml workspace.scripts section.",
            name
        );
    }

    Output::success(&format!("Script '{}' completed", name));
    Ok(())
}

fn run_command(working_dir: &PathBuf, command: &str) -> anyhow::Result<()> {
    let status = Command::new("sh")
        .arg("-c")
        .arg(command)
        .current_dir(working_dir)
        .status()?;

    if !status.success() {
        anyhow::bail!("Command failed with exit code: {:?}", status.code());
    }

    Ok(())
}
