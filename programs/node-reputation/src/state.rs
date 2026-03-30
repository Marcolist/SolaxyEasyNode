use anchor_lang::prelude::*;

/// Maximum length of a metadata URI (IPFS hash or URL).
pub const MAX_METADATA_URI_LEN: usize = 128;

/// Number of heartbeat slots in the ring buffer (24h at 10-min intervals).
pub const HEARTBEAT_RING_SIZE: usize = 144;

/// Number of days used for rolling reputation calculation.
pub const REPUTATION_WINDOW_DAYS: u32 = 7;

// ── Seeds ───────────────────────────────────────────────────────────────────

pub const SEED_NODE: &[u8] = b"node";
pub const SEED_HEARTBEAT: &[u8] = b"heartbeat";
pub const SEED_EPOCH: &[u8] = b"epoch";
pub const SEED_NETWORK: &[u8] = b"network-stats";

// ── Enums ───────────────────────────────────────────────────────────────────

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, InitSpace)]
pub enum NodeRole {
    Sequencer,
    Prover,
    Both,
}

// ── Node Registry ───────────────────────────────────────────────────────────

/// One PDA per registered node. Tracks identity, role, and reputation.
#[account]
#[derive(InitSpace)]
pub struct NodeRegistry {
    /// The operator's signing authority (wallet that registered this node).
    pub authority: Pubkey,
    /// The node's wallet address on Solaxy L2.
    pub solaxy_wallet: Pubkey,
    /// Celestia DA signer address (raw 32 bytes).
    pub celestia_address: [u8; 32],
    /// Node role: Sequencer, Prover, or Both.
    pub role: NodeRole,
    /// Unix timestamp of initial registration.
    pub registered_at: i64,
    /// Unix timestamp of the most recent heartbeat.
    pub last_heartbeat: i64,
    /// Total heartbeats ever submitted.
    pub total_heartbeats: u64,
    /// Heartbeats that were expected but never arrived.
    pub missed_heartbeats: u64,
    /// Reputation score: 0–10000 (represents 0.00%–100.00%).
    pub reputation_score: u16,
    /// Whether the node is currently active.
    pub is_active: bool,
    /// Current write position in the heartbeat ring buffer.
    pub heartbeat_head: u8,
    /// PDA bump seed.
    pub bump: u8,
    /// Optional metadata URI (hostname, geo, description).
    #[max_len(MAX_METADATA_URI_LEN)]
    pub metadata_uri: String,
}

// ── Heartbeat Ring Buffer ───────────────────────────────────────────────────

/// Stores the last 144 heartbeats (24h at 10-min intervals) in a ring buffer.
/// One PDA per node; entries are overwritten cyclically.
#[account]
#[derive(InitSpace)]
pub struct HeartbeatBuffer {
    /// The node this buffer belongs to.
    pub node: Pubkey,
    /// Ring buffer of heartbeat entries.
    #[max_len(HEARTBEAT_RING_SIZE)]
    pub entries: Vec<HeartbeatEntry>,
    /// PDA bump seed.
    pub bump: u8,
}

/// A single heartbeat snapshot.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Default, InitSpace)]
pub struct HeartbeatEntry {
    /// Unix timestamp of this heartbeat.
    pub timestamp: i64,
    /// Current Solaxy rollup block height.
    pub solaxy_block_height: u64,
    /// Current Celestia DAS sampling height.
    pub celestia_das_height: u64,
    /// Bitfield: bit 0 = solaxy, 1 = celestia, 2 = postgres, 3 = rpc.
    pub services_healthy: u8,
    /// Uptime percentage 0–10000.
    pub uptime_pct: u16,
    /// CPU usage 0–100.
    pub cpu_usage: u8,
    /// Number of connected P2P peers.
    pub peer_count: u16,
    /// Latest attested height from attester-incentives module.
    pub attested_height: u64,
}

// ── Epoch Summary ───────────────────────────────────────────────────────────

/// Daily aggregation for one node. Used for rolling reputation calculation.
#[account]
#[derive(InitSpace)]
pub struct EpochSummary {
    /// The node this epoch belongs to.
    pub node: Pubkey,
    /// Day number (unix_timestamp / 86400).
    pub epoch_day: u32,
    /// Expected heartbeats for this day (max 144).
    pub heartbeats_expected: u8,
    /// Actually received heartbeats.
    pub heartbeats_received: u8,
    /// Average uptime over the day (0–10000).
    pub avg_uptime: u16,
    /// Average block sync lag in blocks.
    pub avg_sync_lag: u32,
    /// Minutes any service was unhealthy.
    pub services_downtime_minutes: u16,
    /// Reputation score delta for this day.
    pub score_delta: i16,
    /// PDA bump seed.
    pub bump: u8,
}

// ── Network Stats (Singleton) ───────────────────────────────────────────────

/// Global network statistics. Single PDA, updated on each heartbeat.
#[account]
#[derive(InitSpace)]
pub struct NetworkStats {
    /// Authority that initialized the program.
    pub authority: Pubkey,
    /// Total registered nodes (active + inactive).
    pub total_nodes: u32,
    /// Nodes with a heartbeat in the last 30 minutes.
    pub active_nodes: u32,
    /// Number of registered sequencer nodes.
    pub total_sequencers: u16,
    /// Number of registered prover nodes.
    pub total_provers: u16,
    /// Network-wide average reputation score (0–10000).
    pub avg_network_reputation: u16,
    /// Highest block height reported by any node (reference for sync lag).
    pub max_block_height: u64,
    /// Unix timestamp of last update.
    pub last_updated: i64,
    /// PDA bump seed.
    pub bump: u8,
}
