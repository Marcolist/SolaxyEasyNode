use anchor_lang::prelude::*;

use crate::errors::ReputationError;
use crate::reputation::{calculate_reputation, MIN_HEARTBEAT_INTERVAL};
use crate::state::*;

#[derive(Accounts)]
pub struct SubmitHeartbeat<'info> {
    pub operator: Signer<'info>,

    #[account(
        mut,
        seeds = [SEED_NODE, operator.key().as_ref()],
        bump = node.bump,
        has_one = authority @ ReputationError::Unauthorized,
    )]
    pub node: Account<'info, NodeRegistry>,

    #[account(
        mut,
        seeds = [SEED_HEARTBEAT, operator.key().as_ref()],
        bump = heartbeat_buffer.bump,
    )]
    pub heartbeat_buffer: Account<'info, HeartbeatBuffer>,

    #[account(
        mut,
        seeds = [SEED_NETWORK],
        bump = network_stats.bump,
    )]
    pub network_stats: Account<'info, NetworkStats>,

    /// CHECK: node.authority — verified via has_one constraint above.
    pub authority: UncheckedAccount<'info>,
}

#[allow(clippy::too_many_arguments)]
pub fn handler(
    ctx: Context<SubmitHeartbeat>,
    solaxy_block_height: u64,
    celestia_das_height: u64,
    services_healthy: u8,
    uptime_pct: u16,
    cpu_usage: u8,
    peer_count: u16,
    attested_height: u64,
) -> Result<()> {
    require!(uptime_pct <= 10000, ReputationError::InvalidUptime);
    require!(cpu_usage <= 100, ReputationError::InvalidCpuUsage);

    let node = &ctx.accounts.node;
    require!(node.is_active, ReputationError::NodeNotActive);

    let now = Clock::get()?.unix_timestamp;

    // Rate-limit: minimum 5 minutes between heartbeats
    if node.last_heartbeat > 0 {
        require!(
            now - node.last_heartbeat >= MIN_HEARTBEAT_INTERVAL,
            ReputationError::HeartbeatTooFrequent
        );
    }

    // Write heartbeat into ring buffer
    let buffer = &mut ctx.accounts.heartbeat_buffer;
    let head = ctx.accounts.node.heartbeat_head as usize % HEARTBEAT_RING_SIZE;

    buffer.entries[head] = HeartbeatEntry {
        timestamp: now,
        solaxy_block_height,
        celestia_das_height,
        services_healthy,
        uptime_pct,
        cpu_usage,
        peer_count,
        attested_height,
    };

    // Update node state
    let node = &mut ctx.accounts.node;
    node.last_heartbeat = now;
    node.total_heartbeats += 1;
    node.heartbeat_head = ((head + 1) % HEARTBEAT_RING_SIZE) as u8;

    // Update network max block height
    let stats = &mut ctx.accounts.network_stats;
    if solaxy_block_height > stats.max_block_height {
        stats.max_block_height = solaxy_block_height;
    }

    // Recalculate reputation score
    let new_score = calculate_reputation(node, buffer, stats.max_block_height, now);
    let node = &mut ctx.accounts.node;
    node.reputation_score = new_score;

    // Update network stats timestamp
    stats.last_updated = now;

    Ok(())
}
