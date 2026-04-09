//! `gr channel` — communicate with agents via synapt recall channels.
//!
//! Wraps `synapt recall channel` CLI to provide a human-friendly interface
//! for posting, reading, and searching channel messages from any terminal.

use std::process::Command;

use anyhow::Result;

use crate::cli::args::ChannelCommands;

use super::spawn::find_workspace_root;

// ---------------------------------------------------------------------------
// Core helper
// ---------------------------------------------------------------------------

/// Run `synapt recall channel <args...>` and return stdout.
fn run_synapt_channel(args: &[String], quiet: bool) -> Result<String> {
    let workspace_root = find_workspace_root()?;

    let output = Command::new("synapt")
        .args(["recall", "channel"])
        .args(args)
        .current_dir(&workspace_root)
        .output()
        .map_err(|e| anyhow::anyhow!("Failed to run synapt: {} (is synapt installed?)", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("channel error: {}", stderr.trim());
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();

    if !quiet && !stdout.is_empty() {
        print!("{}", stdout);
    }

    Ok(stdout)
}

fn build_channel_args(action: &ChannelCommands) -> Vec<String> {
    match action {
        ChannelCommands::Post {
            message,
            channel,
            pin,
        } => {
            let mut args = vec!["post".to_string(), channel.clone(), message.clone()];
            if *pin {
                args.push("--pin".to_string());
            }
            args
        }
        ChannelCommands::Read {
            channel,
            limit,
            detail,
        } => vec![
            "read".to_string(),
            channel.clone(),
            "--limit".to_string(),
            limit.to_string(),
            "--detail".to_string(),
            detail.clone(),
        ],
        ChannelCommands::Who => vec!["who".to_string()],
        ChannelCommands::Search { query, channel } => vec![
            "search".to_string(),
            channel.clone().unwrap_or_else(|| "dev".to_string()),
            query.clone(),
        ],
        ChannelCommands::List => vec!["list".to_string()],
        ChannelCommands::Join { channel, name } => {
            let mut args = vec!["join".to_string(), channel.clone()];
            if let Some(name) = name {
                args.extend(["--name".to_string(), name.clone()]);
            }
            args
        }
    }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

pub fn run_channel(action: ChannelCommands, quiet: bool, _json: bool) -> Result<()> {
    let args = build_channel_args(&action);
    run_synapt_channel(&args, quiet)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::build_channel_args;
    use crate::cli::args::ChannelCommands;

    #[test]
    fn post_includes_channel_message_and_pin_flag() {
        let args = build_channel_args(&ChannelCommands::Post {
            message: "hello team".into(),
            channel: "ops".into(),
            pin: true,
        });

        assert_eq!(args, vec!["post", "ops", "hello team", "--pin"]);
    }

    #[test]
    fn read_includes_limit_and_detail() {
        let args = build_channel_args(&ChannelCommands::Read {
            channel: "dev".into(),
            limit: 10,
            detail: "high".into(),
        });

        assert_eq!(args, vec!["read", "dev", "--limit", "10", "--detail", "high"]);
    }

    #[test]
    fn who_has_no_extra_args() {
        let args = build_channel_args(&ChannelCommands::Who);
        assert_eq!(args, vec!["who"]);
    }

    #[test]
    fn search_defaults_to_dev_when_channel_omitted() {
        let args = build_channel_args(&ChannelCommands::Search {
            query: "heartbeat".into(),
            channel: None,
        });

        assert_eq!(args, vec!["search", "dev", "heartbeat"]);
    }

    #[test]
    fn search_uses_explicit_channel_when_provided() {
        let args = build_channel_args(&ChannelCommands::Search {
            query: "heartbeat".into(),
            channel: Some("ops".into()),
        });

        assert_eq!(args, vec!["search", "ops", "heartbeat"]);
    }

    #[test]
    fn list_has_no_extra_args() {
        let args = build_channel_args(&ChannelCommands::List);
        assert_eq!(args, vec!["list"]);
    }

    #[test]
    fn join_omits_name_when_not_provided() {
        let args = build_channel_args(&ChannelCommands::Join {
            channel: "dev".into(),
            name: None,
        });

        assert_eq!(args, vec!["join", "dev"]);
    }

    #[test]
    fn join_includes_name_when_provided() {
        let args = build_channel_args(&ChannelCommands::Join {
            channel: "dev".into(),
            name: Some("Atlas".into()),
        });

        assert_eq!(args, vec!["join", "dev", "--name", "Atlas"]);
    }
}
