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

/// This private worker runs after gr1's program name bypasses the resolver, before the async runtime.
/// It is gr1's own pipe consumer, regardless of the executable's filename or tmux's working directory.
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
    fn quote(value: &str) -> String {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
    fn path(path: &Path) -> io::Result<String> {
        let value = path
            .to_str()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "pane capture path is not UTF-8"))?;
        Ok(quote(value))
    }
    let worker = format!("exec -a gr1 {} {INTERNAL_ARG} {}", path(executable)?, path(log)?);
    Ok(format!("bash -c {}", quote(&worker)))
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn cap_keeps_the_prefix_and_consumes_the_entire_stream() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        let bytes = b"\x1b[2K\rspinner redraws keep arriving".repeat(1000);
        let mut input = Cursor::new(&bytes);
        assert_eq!(capture(&mut input, &path, 17).unwrap(), 17);
        assert_eq!(std::fs::read(&path).unwrap(), &bytes[..17]);
        assert_eq!(input.position(), bytes.len() as u64);
    }

    #[test]
    fn existing_bytes_count_against_the_cap_on_each_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        std::fs::write(&path, b"prefix").unwrap();
        assert_eq!(capture(&mut Cursor::new(b"abcdefghij"), &path, 10).unwrap(), 4);
        assert_eq!(std::fs::read(&path).unwrap(), b"prefixabcd");
        let mut next = Cursor::new(b"more spinner output");
        assert_eq!(capture(&mut next, &path, 10).unwrap(), 0);
        assert_eq!(std::fs::read(&path).unwrap(), b"prefixabcd");
        assert_eq!(next.position(), 19);
    }

    #[test]
    fn oversized_old_log_is_not_shortened_or_appended() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        let old = b"old capture already exceeds the budget";
        std::fs::write(&path, old).unwrap();
        let mut input = Cursor::new(b"new bytes");
        assert_eq!(capture(&mut input, &path, 4).unwrap(), 0);
        assert_eq!(std::fs::read(&path).unwrap(), old);
        assert_eq!(input.position(), 9);
    }

    #[test]
    fn another_owner_refuses_without_waiting_or_appending() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        std::fs::write(&path, b"old").unwrap();
        let owner = OpenOptions::new().append(true).open(&path).unwrap();
        owner.try_lock_exclusive().unwrap();
        assert!(capture(&mut Cursor::new(b"new"), &path, 10).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"old");
        drop(owner);
        assert_eq!(capture(&mut Cursor::new(b"new"), &path, 10).unwrap(), 3);
    }

    #[test]
    fn read_error_is_reported_and_releases_ownership() {
        struct Broken;
        impl Read for Broken {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::new(io::ErrorKind::Other, "input failed"))
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        assert!(capture(&mut Broken, &path, 10).is_err());
        assert_eq!(capture(&mut Cursor::new(b"retry"), &path, 10).unwrap(), 5);
    }

    #[test]
    fn unusable_destination_refuses() {
        let dir = tempfile::tempdir().unwrap();
        assert!(capture(&mut Cursor::new(b"x"), dir.path(), 10).is_err());
        assert!(capture(&mut Cursor::new(b"x"), &dir.path().join("missing/output.log"), 10).is_err());
    }
}
