//! Bounded, rotating capture of a tmux pane's terminal stream.
//!
//! Keep the active 10 MiB and one previous segment. A stable sibling lock owns all rotations.

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
        let value = path.to_str().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "pane capture path is not UTF-8",
            )
        })?;
        Ok(quote(value))
    }
    let worker = format!(
        "exec -a gr1 {} {INTERNAL_ARG} {}",
        path(executable)?,
        path(log)?
    );
    Ok(format!("bash -c {}", quote(&worker)))
}

/// Keep receiving current output after each cap crossing. An old oversized capture rotates intact on the
/// next write; it is replaced by a bounded segment at the following rotation.
pub fn capture(reader: &mut impl Read, path: &Path, limit: u64) -> io::Result<u64> {
    if limit == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "zero pane log cap",
        ));
    }
    let mut lock_path = path.as_os_str().to_os_string();
    lock_path.push(".lock");
    let owner = OpenOptions::new()
        .create(true)
        .append(true)
        .open(lock_path)?;
    // A replaced pipe can start before its predecessor sees EOF. Wait for that owner to finish rather
    // than losing capture to a try-lock race. tmux closes the previous pipe when it replaces it.
    owner.lock_exclusive()?;
    let mut backup = path.as_os_str().to_os_string();
    backup.push(".1");
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let mut size = file.metadata()?.len();
    let mut written = 0;
    let mut buffer = [0_u8; 8192];
    loop {
        let count = match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let mut offset = 0;
        while offset < count {
            if size >= limit {
                file.flush()?;
                drop(file);
                std::fs::rename(path, &backup)?;
                file = OpenOptions::new()
                    .create_new(true)
                    .append(true)
                    .open(path)?;
                size = 0;
            }
            let take = (limit - size).min((count - offset) as u64) as usize;
            file.write_all(&buffer[offset..offset + take])?;
            size += take as u64;
            written += take as u64;
            offset += take;
        }
    }
    file.flush()?;
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn cap_rotates_and_keeps_receiving_the_tail() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        let bytes = b"abcdefghijklmnopqrstuvw";
        let mut input = Cursor::new(&bytes);
        assert_eq!(capture(&mut input, &path, 10).unwrap(), 23);
        assert_eq!(std::fs::read(&path).unwrap(), b"uvw");
        assert_eq!(
            std::fs::read(dir.path().join("output.log.1")).unwrap(),
            b"klmnopqrst"
        );
        assert_eq!(input.position(), bytes.len() as u64);
    }

    #[test]
    fn existing_bytes_count_against_the_cap_on_each_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        std::fs::write(&path, b"prefix").unwrap();
        assert_eq!(
            capture(&mut Cursor::new(b"abcdefghij"), &path, 10).unwrap(),
            10
        );
        assert_eq!(std::fs::read(&path).unwrap(), b"efghij");
        assert_eq!(
            std::fs::read(dir.path().join("output.log.1")).unwrap(),
            b"prefixabcd"
        );
        assert_eq!(capture(&mut Cursor::new(b"klmnop"), &path, 10).unwrap(), 6);
        assert_eq!(std::fs::read(&path).unwrap(), b"op");
        assert_eq!(
            std::fs::read(dir.path().join("output.log.1")).unwrap(),
            b"efghijklmn"
        );
    }

    #[test]
    fn oversized_old_log_rotates_intact_before_new_output() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        let old = b"old capture already exceeds the budget";
        std::fs::write(&path, old).unwrap();
        let mut input = Cursor::new(b"new");
        assert_eq!(capture(&mut input, &path, 4).unwrap(), 3);
        assert_eq!(std::fs::read(&path).unwrap(), b"new");
        assert_eq!(std::fs::read(dir.path().join("output.log.1")).unwrap(), old);
        assert_eq!(input.position(), 3);
    }

    #[test]
    fn replacement_waits_for_stable_owner_then_captures() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("output.log");
        std::fs::write(&path, b"old").unwrap();
        let owner = OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.path().join("output.log.lock"))
            .unwrap();
        owner.try_lock_exclusive().unwrap();
        let (send, recv) = std::sync::mpsc::channel();
        let worker_path = path.clone();
        let worker = std::thread::spawn(move || {
            send.send(capture(&mut Cursor::new(b"new"), &worker_path, 10))
                .unwrap();
        });
        assert!(recv
            .recv_timeout(std::time::Duration::from_millis(50))
            .is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"old");
        drop(owner);
        assert_eq!(
            recv.recv_timeout(std::time::Duration::from_secs(2))
                .unwrap()
                .unwrap(),
            3
        );
        worker.join().unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"oldnew");
    }

    #[test]
    fn read_error_is_reported_and_releases_ownership() {
        struct Broken;
        impl Read for Broken {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("input failed"))
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
        assert!(capture(
            &mut Cursor::new(b"x"),
            &dir.path().join("missing/output.log"),
            10
        )
        .is_err());
    }
}
