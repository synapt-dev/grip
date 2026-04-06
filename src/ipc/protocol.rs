//! Wake coordination protocol messages.
//!
//! Replaces codi-rs's permission/tool protocol with wake-specific messages
//! for the WakeCoordinator pattern.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Wake priority levels (from WakeCoordinator design).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WakePriority {
    /// Interval-based polling fallback.
    Interval,
    /// Channel activity (new messages in a watched channel).
    ChannelActivity,
    /// Queued work changed (new tasks, PR updates).
    QueuedWork,
    /// Retry after a previous failed attempt.
    Retry,
    /// Direct user action or @mention — highest priority.
    UserAction,
}

/// Why an agent should wake up.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WakeReason {
    /// Priority of this wake event.
    pub priority: WakePriority,
    /// Channel that triggered the wake (if applicable).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channel: Option<String>,
    /// Human-readable description.
    pub description: String,
    /// Wake sequence number (for ack ordering).
    pub seq: u64,
    /// Timestamp of the wake event.
    pub timestamp: DateTime<Utc>,
}

/// Messages from agent processes to the coordinator.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AgentMessage {
    /// Initial handshake from agent.
    Handshake {
        agent_id: String,
        /// Channels this agent watches.
        watch_channels: Vec<String>,
        /// Wake targets (channel names, agent mentions).
        watch_targets: Vec<String>,
    },
    /// Acknowledge processed wakes up to a sequence number.
    Ack { up_to_seq: u64 },
    /// Agent is alive (response to ping).
    Pong,
    /// Agent is shutting down gracefully.
    Shutdown,
}

/// Messages from the coordinator to agent processes.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CoordinatorMessage {
    /// Accept agent connection.
    HandshakeAck {
        accepted: bool,
        /// Fallback poll interval in seconds.
        fallback_interval_s: u64,
    },
    /// Wake the agent — something needs attention.
    Wake { reason: WakeReason },
    /// Health check.
    Ping,
    /// Coordinator is shutting down.
    Shutdown,
}

/// Encode a message as NDJSON (newline-delimited JSON).
pub fn encode<T: Serialize>(msg: &T) -> Result<String, serde_json::Error> {
    let mut s = serde_json::to_string(msg)?;
    s.push('\n');
    Ok(s)
}

/// Decode a single NDJSON line.
pub fn decode<T: for<'de> Deserialize<'de>>(line: &str) -> Result<T, serde_json::Error> {
    serde_json::from_str(line.trim())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_decode_agent_handshake() {
        let msg = AgentMessage::Handshake {
            agent_id: "atlas".to_string(),
            watch_channels: vec!["dev".to_string()],
            watch_targets: vec!["@atlas".to_string()],
        };
        let encoded = encode(&msg).unwrap();
        assert!(encoded.ends_with('\n'));

        let decoded: AgentMessage = decode(&encoded).unwrap();
        match decoded {
            AgentMessage::Handshake { agent_id, .. } => assert_eq!(agent_id, "atlas"),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn test_encode_decode_wake() {
        let msg = CoordinatorMessage::Wake {
            reason: WakeReason {
                priority: WakePriority::UserAction,
                channel: Some("dev".to_string()),
                description: "@atlas mentioned in #dev".to_string(),
                seq: 42,
                timestamp: Utc::now(),
            },
        };
        let encoded = encode(&msg).unwrap();
        let decoded: CoordinatorMessage = decode(&encoded).unwrap();
        match decoded {
            CoordinatorMessage::Wake { reason } => {
                assert_eq!(reason.priority, WakePriority::UserAction);
                assert_eq!(reason.seq, 42);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn test_wake_priority_ordering() {
        assert!(WakePriority::UserAction > WakePriority::ChannelActivity);
        assert!(WakePriority::ChannelActivity > WakePriority::Interval);
        assert!(WakePriority::Retry > WakePriority::Interval);
    }

    #[test]
    fn test_encode_decode_ack() {
        let msg = AgentMessage::Ack { up_to_seq: 99 };
        let encoded = encode(&msg).unwrap();
        let decoded: AgentMessage = decode(&encoded).unwrap();
        match decoded {
            AgentMessage::Ack { up_to_seq } => assert_eq!(up_to_seq, 99),
            _ => panic!("wrong variant"),
        }
    }
}
