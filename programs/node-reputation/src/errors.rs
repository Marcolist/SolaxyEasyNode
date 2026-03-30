use anchor_lang::prelude::*;

#[error_code]
pub enum ReputationError {
    #[msg("Node is not active")]
    NodeNotActive,
    #[msg("Node is already active")]
    NodeAlreadyActive,
    #[msg("Heartbeat submitted too soon (minimum 5 minutes between heartbeats)")]
    HeartbeatTooFrequent,
    #[msg("Uptime percentage must be between 0 and 10000")]
    InvalidUptime,
    #[msg("CPU usage must be between 0 and 100")]
    InvalidCpuUsage,
    #[msg("Metadata URI exceeds maximum length")]
    MetadataUriTooLong,
    #[msg("Epoch day is in the future")]
    EpochInFuture,
    #[msg("Epoch day does not match any recorded heartbeats")]
    EpochNoData,
    #[msg("Unauthorized: only the node operator can perform this action")]
    Unauthorized,
}
