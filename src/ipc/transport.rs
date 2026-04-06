//! Cross-platform transport for IPC.
//!
//! Ported from codi-rs with minimal changes. Abstracts Unix domain sockets
//! and Windows named pipes behind a common async IO trait.

use std::io;
use std::path::Path;

use tokio::io::{AsyncRead, AsyncWrite};

/// Trait alias for async read+write streams.
pub trait IpcIo: AsyncRead + AsyncWrite + Unpin + Send {}

impl<T> IpcIo for T where T: AsyncRead + AsyncWrite + Unpin + Send {}

/// Type-erased IPC stream.
pub type IpcStream = Box<dyn IpcIo>;

#[cfg(unix)]
use tokio::net::UnixListener;
#[cfg(unix)]
use tokio::net::UnixStream;

/// IPC listener that accepts incoming connections.
pub struct IpcListener {
    #[cfg(unix)]
    inner: UnixListener,
    #[cfg(windows)]
    name: String,
    #[cfg(windows)]
    first: std::sync::Mutex<Option<tokio::net::windows::named_pipe::NamedPipeServer>>,
}

/// Bind a listener to a socket/pipe path.
pub async fn bind(path: &Path) -> io::Result<IpcListener> {
    #[cfg(unix)]
    {
        if path.exists() {
            let _ = std::fs::remove_file(path);
        }

        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }

        let inner = UnixListener::bind(path)?;
        Ok(IpcListener { inner })
    }

    #[cfg(windows)]
    {
        use tokio::net::windows::named_pipe::ServerOptions;
        let name = pipe_name_from_path(path);
        // Create the first pipe instance — must use first_pipe_instance(true).
        let first = ServerOptions::new()
            .first_pipe_instance(true)
            .create(&name)?;
        Ok(IpcListener {
            name,
            first: std::sync::Mutex::new(Some(first)),
        })
    }
}

/// Connect to a socket/pipe as a client.
pub async fn connect(path: &Path) -> io::Result<IpcStream> {
    #[cfg(unix)]
    {
        let stream = UnixStream::connect(path).await?;
        Ok(Box::new(stream))
    }

    #[cfg(windows)]
    {
        use tokio::net::windows::named_pipe::ClientOptions;
        let name = pipe_name_from_path(path);
        let pipe = ClientOptions::new().open(&name)?;
        Ok(Box::new(pipe))
    }
}

impl IpcListener {
    /// Accept a new connection.
    pub async fn accept(&self) -> io::Result<IpcStream> {
        #[cfg(unix)]
        {
            let (stream, _addr) = self.inner.accept().await?;
            Ok(Box::new(stream))
        }

        #[cfg(windows)]
        {
            use tokio::net::windows::named_pipe::ServerOptions;
            // Use the pre-created first instance if available, otherwise create a new one.
            let server = if let Some(first) = self.first.lock().unwrap().take() {
                first
            } else {
                ServerOptions::new()
                    .first_pipe_instance(false)
                    .create(&self.name)?
            };
            server.connect().await?;
            Ok(Box::new(server))
        }
    }
}

#[cfg(windows)]
fn pipe_name_from_path(path: &Path) -> String {
    let name = path.to_string_lossy().replace('/', "-").replace('\\', "-");
    format!(r"\\.\pipe\gitgrip-{}", name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[cfg(unix)]
    #[tokio::test]
    async fn test_unix_socket_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let sock = dir.path().join("test.sock");

        let listener = bind(&sock).await.unwrap();

        let client_task = tokio::spawn({
            let sock = sock.clone();
            async move {
                let mut stream = connect(&sock).await.unwrap();
                stream.write_all(b"hello\n").await.unwrap();

                let mut buf = vec![0u8; 64];
                let n = stream.read(&mut buf).await.unwrap();
                String::from_utf8_lossy(&buf[..n]).to_string()
            }
        });

        let mut server_stream = listener.accept().await.unwrap();
        let mut buf = vec![0u8; 64];
        let n = server_stream.read(&mut buf).await.unwrap();
        assert_eq!(&buf[..n], b"hello\n");

        server_stream.write_all(b"world\n").await.unwrap();

        let response = client_task.await.unwrap();
        assert_eq!(response, "world\n");
    }
}
