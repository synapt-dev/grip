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
fn run_synapt_channel(args: &[&str], quiet: bool) -> Result<String> {
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

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

pub fn run_channel(action: ChannelCommands, quiet: bool, _json: bool) -> Result<()> {
    match action {
        ChannelCommands::Post {
            message,
            channel,
            pin,
            name,
        } => run_channel_post(&message, &channel, pin, name.as_deref(), quiet),

        ChannelCommands::Read {
            channel,
            limit,
            detail,
        } => run_channel_read(&channel, limit, &detail, quiet),

        ChannelCommands::Who => run_channel_who(quiet),

        ChannelCommands::Search { query, channel } => {
            run_channel_search(&query, channel.as_deref(), quiet)
        }

        ChannelCommands::List => run_channel_list(quiet),

        ChannelCommands::Join { channel, name } => {
            run_channel_join(&channel, name.as_deref(), quiet)
        }
    }
}

// ---------------------------------------------------------------------------
// Subcommands
// ---------------------------------------------------------------------------

/// Post a message to a channel.
fn run_channel_post(
    message: &str,
    channel: &str,
    pin: bool,
    name: Option<&str>,
    quiet: bool,
) -> Result<()> {
    let mut args: Vec<&str> = vec!["post", channel, message];
    if pin {
        args.push("--pin");
    }
    // Join with display name before posting if --name provided
    if let Some(n) = name {
        run_synapt_channel(&["join", channel, "--name", n], true)?;
    }
    run_synapt_channel(&args, quiet)?;
    Ok(())
}

/// Read recent messages from a channel.
fn run_channel_read(channel: &str, limit: u32, detail: &str, quiet: bool) -> Result<()> {
    let limit_str = limit.to_string();
    let mut args = vec!["read", channel, "--limit", &limit_str];
    args.extend(["--detail", detail]);
    run_synapt_channel(&args, quiet)?;
    Ok(())
}

/// Join a channel with an optional display name.
fn run_channel_join(channel: &str, name: Option<&str>, quiet: bool) -> Result<()> {
    let mut args = vec!["join", channel];
    if let Some(n) = name {
        args.extend(["--name", n]);
    }
    run_synapt_channel(&args, quiet)?;
    Ok(())
}

/// Show who's online.
fn run_channel_who(quiet: bool) -> Result<()> {
    run_synapt_channel(&["who"], quiet)?;
    Ok(())
}

/// Search across channel history.
fn run_channel_search(query: &str, channel: Option<&str>, quiet: bool) -> Result<()> {
    // synapt CLI requires a channel positional; default to "dev" when omitted
    let ch = channel.unwrap_or("dev");
    let args = vec!["search", ch, query];
    run_synapt_channel(&args, quiet)?;
    Ok(())
}

/// List all channels.
fn run_channel_list(quiet: bool) -> Result<()> {
    run_synapt_channel(&["list"], quiet)?;
    Ok(())
}
