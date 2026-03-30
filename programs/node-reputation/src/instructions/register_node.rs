use anchor_lang::prelude::*;

use crate::errors::ReputationError;
use crate::state::*;

#[derive(Accounts)]
pub struct RegisterNode<'info> {
    #[account(mut)]
    pub operator: Signer<'info>,

    #[account(
        init,
        payer = operator,
        space = 8 + NodeRegistry::INIT_SPACE,
        seeds = [SEED_NODE, operator.key().as_ref()],
        bump,
    )]
    pub node: Account<'info, NodeRegistry>,

    #[account(
        init,
        payer = operator,
        space = 8 + HeartbeatBuffer::INIT_SPACE,
        seeds = [SEED_HEARTBEAT, operator.key().as_ref()],
        bump,
    )]
    pub heartbeat_buffer: Account<'info, HeartbeatBuffer>,

    #[account(
        mut,
        seeds = [SEED_NETWORK],
        bump = network_stats.bump,
    )]
    pub network_stats: Account<'info, NetworkStats>,

    pub system_program: Program<'info, System>,
}

pub fn process(
    ctx: Context<RegisterNode>,
    solaxy_wallet: Pubkey,
    celestia_address: [u8; 32],
    role: NodeRole,
    metadata_uri: String,
) -> Result<()> {
    require!(
        metadata_uri.len() <= MAX_METADATA_URI_LEN,
        ReputationError::MetadataUriTooLong
    );

    let now = Clock::get()?.unix_timestamp;

    // Initialize node registry
    let node = &mut ctx.accounts.node;
    node.authority = ctx.accounts.operator.key();
    node.solaxy_wallet = solaxy_wallet;
    node.celestia_address = celestia_address;
    node.role = role;
    node.registered_at = now;
    node.last_heartbeat = 0;
    node.total_heartbeats = 0;
    node.missed_heartbeats = 0;
    node.reputation_score = 0;
    node.is_active = true;
    node.heartbeat_head = 0;
    node.bump = ctx.bumps.node;
    node.metadata_uri = metadata_uri;

    // Initialize heartbeat buffer
    let buffer = &mut ctx.accounts.heartbeat_buffer;
    buffer.node = ctx.accounts.operator.key();
    buffer.entries = vec![HeartbeatEntry::default(); HEARTBEAT_RING_SIZE];
    buffer.bump = ctx.bumps.heartbeat_buffer;

    // Update network stats
    let stats = &mut ctx.accounts.network_stats;
    stats.total_nodes += 1;
    stats.active_nodes += 1;
    match role {
        NodeRole::Sequencer => stats.total_sequencers += 1,
        NodeRole::Prover => stats.total_provers += 1,
        NodeRole::Both => {
            stats.total_sequencers += 1;
            stats.total_provers += 1;
        }
    }
    stats.last_updated = now;

    Ok(())
}
