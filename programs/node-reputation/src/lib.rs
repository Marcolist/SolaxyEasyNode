use anchor_lang::prelude::*;

pub mod errors;
pub mod instructions;
pub mod reputation;
pub mod state;

use instructions::*;

declare_id!("RepNodE1111111111111111111111111111111111111");

#[program]
pub mod node_reputation {
    use super::*;

    /// Initialize the global network stats singleton. Called once after deploy.
    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        instructions::initialize::handler(ctx)
    }

    /// Register a new node in the on-chain registry.
    pub fn register_node(
        ctx: Context<RegisterNode>,
        solaxy_wallet: Pubkey,
        celestia_address: [u8; 32],
        role: NodeRole,
        metadata_uri: String,
    ) -> Result<()> {
        instructions::register_node::handler(ctx, solaxy_wallet, celestia_address, role, metadata_uri)
    }

    /// Submit a periodic heartbeat with current node metrics.
    pub fn submit_heartbeat(
        ctx: Context<SubmitHeartbeat>,
        solaxy_block_height: u64,
        celestia_das_height: u64,
        services_healthy: u8,
        uptime_pct: u16,
        cpu_usage: u8,
        peer_count: u16,
        attested_height: u64,
    ) -> Result<()> {
        instructions::submit_heartbeat::handler(
            ctx,
            solaxy_block_height,
            celestia_das_height,
            services_healthy,
            uptime_pct,
            cpu_usage,
            peer_count,
            attested_height,
        )
    }

    /// Finalize a daily epoch summary for a node. Permissionless crank.
    pub fn close_epoch(ctx: Context<CloseEpoch>, epoch_day: u32) -> Result<()> {
        instructions::close_epoch::handler(ctx, epoch_day)
    }

    /// Update node metadata URI.
    pub fn update_metadata(ctx: Context<UpdateMetadata>, metadata_uri: String) -> Result<()> {
        instructions::update_metadata::handler(ctx, metadata_uri)
    }

    /// Deactivate a node (operator only).
    pub fn deactivate_node(ctx: Context<DeactivateNode>) -> Result<()> {
        instructions::deactivate_node::handler(ctx)
    }
}
