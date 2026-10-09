//! Bounded, append-only capture of a tmux pane's terminal stream.
//!
//! Keep the first 10 MiB, including any bytes already present, then drain without writing. Existing logs are
//! never shortened or rotated. One writer owns the file at a time, including while it drains at the cap.

use fs2::FileExt;
use std::ffi::OsStr;
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::path::Path;

pub const LIMIT_BYTES: u64 = 10 * 1024 * 1024;
const INTERNAL_ARG: &str = "--internal-pane-log";

/// This private worker must run before the command resolver or async runtime. It is gr1's own pipe consumer,
/// regardless of the executable's name or the workspace in which tmux starts it.
pub fn entry() -> Option<i32> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(OsStr::new(INTERNAL_ARG)) {
        return None;
    }
    let path = match (args.next(), args.next()) {
        (Some(path), None) => path,
        _ => {
            eprintln!("gr: pane capture needs exactly one log path");
            return Some(2);
        }
    };
    match capture(&mut io::stdin().lock(), Path::new(&path), LIMIT_BYTES) {
        Ok(_) => Some(0),
        Err(error) => {
            eprintln!("gr: pane capture failed: {error}");
            Some(1)
        }
    }
}

/// Shell command for tmux pipe-pane. Reject non-UTF-8 paths instead of capturing into a lossy filename.
pub fn pipe_command(executable: &Path, log: &Path) -> io::Result<String> {
    fn quote(path: &Path) -> io::Result<String> {
        let value = path
            .to_str()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "pane capture path is not UTF-8"))?;
        Ok(format!("'{}'", value.replace('\'', "'\\''")))
    }
    Ok(format!(
        "{} {INTERNAL_ARG} {}",
        quote(executable)?,
        quote(log)?
    ))
}

/// Append up to the remaining budget. Continue consuming stdin after the cap so pane output does not block.
/// An already oversized log is preserved unchanged; the worker does not clean up older captures.
pub fn capture(reader: &mut impl Read, path: &Path, limit: u64) -> io::Result<u64> {
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    file.try_lock_exclusive()?;
    let mut remaining = limit.saturating_sub(file.metadata()?.len());
    let mut written = 0;
    let mut buffer = [0_u8; 8192];
    loop {
        let count = match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let take = remaining.min(count as u64) as usize;
        file.write_all(&buffer[..take])?;
        remaining -= take as u64;
        written += take as u64;
    }
    file.flush()?;
    Ok(written)
}
