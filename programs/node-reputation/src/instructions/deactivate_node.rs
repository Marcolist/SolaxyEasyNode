use anchor_lang::prelude::*;

use crate::errors::ReputationError;
use crate::state::*;

#[derive(Accounts)]
pub struct DeactivateNode<'info> {
    pub operator: Signer<'info>,

    #[account(
        mut,
        seeds = [SEED_NODE, operator.key().as_ref()],
        bump = node.bump,
    )]
    pub node: Account<'info, NodeRegistry>,

    #[account(
        mut,
        seeds = [SEED_NETWORK],
        bump = network_stats.bump,
    )]
    pub network_stats: Account<'info, NetworkStats>,
}

pub fn process(ctx: Context<DeactivateNode>) -> Result<()> {
    let node = &mut ctx.accounts.node;
    require!(node.is_active, ReputationError::NodeNotActive);

    node.is_active = false;

    let stats = &mut ctx.accounts.network_stats;
    stats.active_nodes = stats.active_nodes.saturating_sub(1);
    match node.role {
        NodeRole::Sequencer => stats.total_sequencers = stats.total_sequencers.saturating_sub(1),
        NodeRole::Prover => stats.total_provers = stats.total_provers.saturating_sub(1),
        NodeRole::Both => {
            stats.total_sequencers = stats.total_sequencers.saturating_sub(1);
            stats.total_provers = stats.total_provers.saturating_sub(1);
        }
    }
    stats.last_updated = Clock::get()?.unix_timestamp;

    Ok(())
}
