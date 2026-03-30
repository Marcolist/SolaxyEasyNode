use anchor_lang::prelude::*;

pub mod errors;
pub mod instructions;
pub mod reputation;
pub mod state;

use instructions::*;
use state::NodeRole;

declare_id!("ChD5eVepwaTMEabHWxyfDNCjbyVGx5bphCjoCuXsZw65");

#[program]
pub mod node_reputation {
    use super::*;

    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        instructions::initialize::process(ctx)
    }

    pub fn register_node(
        ctx: Context<RegisterNode>,
        solaxy_wallet: Pubkey,
        celestia_address: [u8; 32],
        role: NodeRole,
        metadata_uri: String,
    ) -> Result<()> {
        instructions::register_node::process(ctx, solaxy_wallet, celestia_address, role, metadata_uri)
    }

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
        instructions::submit_heartbeat::process(
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

    pub fn close_epoch(ctx: Context<CloseEpoch>, epoch_day: u32) -> Result<()> {
        instructions::close_epoch::process(ctx, epoch_day)
    }

    pub fn update_metadata(ctx: Context<UpdateMetadata>, metadata_uri: String) -> Result<()> {
        instructions::update_metadata::process(ctx, metadata_uri)
    }

    pub fn deactivate_node(ctx: Context<DeactivateNode>) -> Result<()> {
        instructions::deactivate_node::process(ctx)
    }
}
