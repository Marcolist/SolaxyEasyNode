use anchor_lang::prelude::*;

use crate::state::*;

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub authority: Signer<'info>,

    #[account(
        init,
        payer = authority,
        space = 8 + NetworkStats::INIT_SPACE,
        seeds = [SEED_NETWORK],
        bump,
    )]
    pub network_stats: Account<'info, NetworkStats>,

    pub system_program: Program<'info, System>,
}

pub fn process(ctx: Context<Initialize>) -> Result<()> {
    let stats = &mut ctx.accounts.network_stats;
    stats.authority = ctx.accounts.authority.key();
    stats.total_nodes = 0;
    stats.active_nodes = 0;
    stats.total_sequencers = 0;
    stats.total_provers = 0;
    stats.avg_network_reputation = 0;
    stats.max_block_height = 0;
    stats.last_updated = Clock::get()?.unix_timestamp;
    stats.bump = ctx.bumps.network_stats;
    Ok(())
}
