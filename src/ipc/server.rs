//! IPC server for the wake coordinator.
//!
//! Listens on a Unix domain socket and manages connected agent sessions.
//! Ported from codi-rs IpcServer with wake-specific message handling.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, RwLock};
use tracing::{debug, error, info, warn};

use super::error::{IpcError, IpcResult};
use super::protocol::{encode, decode, AgentMessage, CoordinatorMessage, WakeReason};
use super::transport::{self, IpcListener, IpcStream};

/// Connected agent session.
struct AgentSession {
    agent_id: String,
    watch_channels: Vec<String>,
    watch_targets: Vec<String>,
    writer: tokio::sync::Mutex<Box<dyn tokio::io::AsyncWrite + Unpin + Send>>,
}

/// Events emitted by the IPC server for the coordinator to handle.
#[derive(Debug)]
pub enum ServerEvent {
    /// Agent connected and completed handshake.
    AgentConnected {
        agent_id: String,
        watch_channels: Vec<String>,
        watch_targets: Vec<String>,
    },
    /// Agent acknowledged wakes up to a sequence number.
    AgentAck {
        agent_id: String,
        up_to_seq: u64,
    },
    /// Agent disconnected.
    AgentDisconnected {
        agent_id: String,
    },
}

/// IPC server that accepts agent connections and dispatches wake messages.
pub struct IpcServer {
    socket_path: PathBuf,
    agents: Arc<RwLock<HashMap<String, Arc<AgentSession>>>>,
    event_tx: mpsc::Sender<ServerEvent>,
    event_rx: Option<mpsc::Receiver<ServerEvent>>,
}

impl IpcServer {
    /// Create a new IPC server.
    pub fn new(socket_path: &Path) -> Self {
        let (tx, rx) = mpsc::channel(64);
        Self {
            socket_path: socket_path.to_path_buf(),
            agents: Arc::new(RwLock::new(HashMap::new())),
            event_tx: tx,
            event_rx: Some(rx),
        }
    }

    /// Take the event receiver. Call this once before starting the server.
    pub fn take_event_rx(&mut self) -> Option<mpsc::Receiver<ServerEvent>> {
        self.event_rx.take()
    }

    /// Start accepting connections in a background task.
    pub async fn start(&self) -> IpcResult<()> {
        let listener = transport::bind(&self.socket_path).await?;
        let agents = Arc::clone(&self.agents);
        let event_tx = self.event_tx.clone();

        info!("IPC server listening on {:?}", self.socket_path);

        tokio::spawn(async move {
            Self::accept_loop(listener, agents, event_tx).await;
        });

        Ok(())
    }

    /// Send a wake message to a specific agent.
    pub async fn send_wake(&self, agent_id: &str, reason: WakeReason) -> IpcResult<()> {
        let agents = self.agents.read().await;
        let session = agents
            .get(agent_id)
            .ok_or_else(|| IpcError::Protocol(format!("agent not connected: {}", agent_id)))?;

        let msg = CoordinatorMessage::Wake { reason };
        let encoded = encode(&msg)?;

        let mut writer = session.writer.lock().await;
        writer
            .write_all(encoded.as_bytes())
            .await
            .map_err(IpcError::Io)?;
        writer.flush().await.map_err(IpcError::Io)?;

        Ok(())
    }

    /// Broadcast a wake to all connected agents watching a specific channel.
    pub async fn broadcast_channel_wake(&self, channel: &str, reason: WakeReason) {
        let agents = self.agents.read().await;
        for (id, session) in agents.iter() {
            if session.watch_channels.iter().any(|c| c == channel) {
                let msg = CoordinatorMessage::Wake {
                    reason: reason.clone(),
                };
                if let Ok(encoded) = encode(&msg) {
                    let mut writer = session.writer.lock().await;
                    if let Err(e) = writer.write_all(encoded.as_bytes()).await {
                        warn!("Failed to send wake to {}: {}", id, e);
                    }
                    let _ = writer.flush().await;
                }
            }
        }
    }

    /// Get the list of connected agent IDs.
    pub async fn connected_agents(&self) -> Vec<String> {
        self.agents.read().await.keys().cloned().collect()
    }

    /// Internal accept loop.
    async fn accept_loop(
        listener: IpcListener,
        agents: Arc<RwLock<HashMap<String, Arc<AgentSession>>>>,
        event_tx: mpsc::Sender<ServerEvent>,
    ) {
        loop {
            match listener.accept().await {
                Ok(stream) => {
                    let agents = Arc::clone(&agents);
                    let event_tx = event_tx.clone();
                    tokio::spawn(async move {
                        if let Err(e) =
                            Self::handle_connection(stream, agents, event_tx).await
                        {
                            debug!("Connection ended: {}", e);
                        }
                    });
                }
                Err(e) => {
                    error!("Accept failed: {}", e);
                    break;
                }
            }
        }
    }

    /// Handle a single agent connection.
    async fn handle_connection(
        stream: IpcStream,
        agents: Arc<RwLock<HashMap<String, Arc<AgentSession>>>>,
        event_tx: mpsc::Sender<ServerEvent>,
    ) -> IpcResult<()> {
        let (reader, writer) = tokio::io::split(stream);
        let mut lines = BufReader::new(reader).lines();

        // Wait for handshake.
        let first_line = lines
            .next_line()
            .await
            .map_err(IpcError::Io)?
            .ok_or(IpcError::ConnectionClosed)?;

        let handshake: AgentMessage = decode(&first_line)?;
        let (agent_id, watch_channels, watch_targets) = match handshake {
            AgentMessage::Handshake {
                agent_id,
                watch_channels,
                watch_targets,
            } => (agent_id, watch_channels, watch_targets),
            _ => {
                return Err(IpcError::HandshakeFailed(
                    "expected Handshake message".to_string(),
                ))
            }
        };

        info!("Agent connected: {}", agent_id);

        // Send handshake ack.
        let ack = CoordinatorMessage::HandshakeAck {
            accepted: true,
            fallback_interval_s: 120,
        };
        let writer = Box::new(writer);
        let writer_mutex = tokio::sync::Mutex::new(writer as Box<dyn tokio::io::AsyncWrite + Unpin + Send>);

        {
            let mut w = writer_mutex.lock().await;
            let encoded = encode(&ack)?;
            w.write_all(encoded.as_bytes()).await.map_err(IpcError::Io)?;
            w.flush().await.map_err(IpcError::Io)?;
        }

        let session = Arc::new(AgentSession {
            agent_id: agent_id.clone(),
            watch_channels: watch_channels.clone(),
            watch_targets: watch_targets.clone(),
            writer: writer_mutex,
        });

        agents.write().await.insert(agent_id.clone(), session);

        let _ = event_tx
            .send(ServerEvent::AgentConnected {
                agent_id: agent_id.clone(),
                watch_channels,
                watch_targets,
            })
            .await;

        // Read loop for subsequent messages.
        while let Ok(Some(line)) = lines.next_line().await {
            match decode::<AgentMessage>(&line) {
                Ok(AgentMessage::Ack { up_to_seq }) => {
                    let _ = event_tx
                        .send(ServerEvent::AgentAck {
                            agent_id: agent_id.clone(),
                            up_to_seq,
                        })
                        .await;
                }
                Ok(AgentMessage::Pong) => {
                    debug!("Pong from {}", agent_id);
                }
                Ok(AgentMessage::Shutdown) => {
                    info!("Agent {} shutting down", agent_id);
                    break;
                }
                Ok(AgentMessage::Handshake { .. }) => {
                    warn!("Duplicate handshake from {}", agent_id);
                }
                Err(e) => {
                    warn!("Bad message from {}: {}", agent_id, e);
                }
            }
        }

        // Cleanup on disconnect.
        agents.write().await.remove(&agent_id);
        let _ = event_tx
            .send(ServerEvent::AgentDisconnected {
                agent_id: agent_id.clone(),
            })
            .await;

        info!("Agent disconnected: {}", agent_id);
        Ok(())
    }
}

impl Drop for IpcServer {
    fn drop(&mut self) {
        // Clean up socket file.
        let _ = std::fs::remove_file(&self.socket_path);
    }
}
