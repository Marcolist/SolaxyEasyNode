use anchor_lang::prelude::*;

use crate::errors::ReputationError;
use crate::state::*;

#[derive(Accounts)]
#[instruction(epoch_day: u32)]
pub struct CloseEpoch<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        seeds = [SEED_NODE, node.authority.as_ref()],
        bump = node.bump,
    )]
    pub node: Account<'info, NodeRegistry>,

    #[account(
        seeds = [SEED_HEARTBEAT, node.authority.as_ref()],
        bump = heartbeat_buffer.bump,
    )]
    pub heartbeat_buffer: Account<'info, HeartbeatBuffer>,

    #[account(
        init,
        payer = payer,
        space = 8 + EpochSummary::INIT_SPACE,
        seeds = [SEED_EPOCH, node.authority.as_ref(), &epoch_day.to_le_bytes()],
        bump,
    )]
    pub epoch_summary: Account<'info, EpochSummary>,

    pub system_program: Program<'info, System>,
}

pub fn process(ctx: Context<CloseEpoch>, epoch_day: u32) -> Result<()> {
    let now = Clock::get()?.unix_timestamp;
    let current_day = (now / 86400) as u32;
    require!(epoch_day <= current_day, ReputationError::EpochInFuture);

    let day_start = epoch_day as i64 * 86400;
    let day_end = day_start + 86400;

    // Collect heartbeats that fall within this epoch day
    let buffer = &ctx.accounts.heartbeat_buffer;
    let day_entries: Vec<&HeartbeatEntry> = buffer
        .entries
        .iter()
        .filter(|e| e.timestamp >= day_start && e.timestamp < day_end)
        .collect();

    require!(!day_entries.is_empty(), ReputationError::EpochNoData);

    let received = day_entries.len() as u8;

    // Calculate expected heartbeats: 144 for a full day, proportional for partial
    let expected: u8 = if epoch_day == current_day {
        // Partial day — scale by elapsed time
        let elapsed = now - day_start;
        ((elapsed as u64 * 144) / 86400).min(144) as u8
    } else {
        144
    };

    // Average uptime
    let avg_uptime = (day_entries.iter().map(|e| e.uptime_pct as u64).sum::<u64>()
        / day_entries.len() as u64) as u16;

    // Average sync lag (blocks behind max height in buffer)
    let max_height = day_entries
        .iter()
        .map(|e| e.solaxy_block_height)
        .max()
        .unwrap_or(0);
    let avg_sync_lag = if max_height == 0 {
        0u32
    } else {
        let total_lag: u64 = day_entries
            .iter()
            .map(|e| max_height.saturating_sub(e.solaxy_block_height))
            .sum();
        (total_lag / day_entries.len() as u64) as u32
    };

    // Service downtime: count heartbeats where any service was unhealthy, * 10 min
    let unhealthy_count = day_entries
        .iter()
        .filter(|e| e.services_healthy != 0x0F)
        .count();
    let services_downtime_minutes = (unhealthy_count as u16) * 10;

    // Score delta (simplified: positive if performing well, negative if not)
    let uptime_ratio = if expected > 0 {
        (received as i16 * 100) / expected as i16
    } else {
        0
    };
    let score_delta = uptime_ratio - 50; // > 50% heartbeats = positive

    // Write epoch summary
    let summary = &mut ctx.accounts.epoch_summary;
    summary.node = ctx.accounts.node.authority;
    summary.epoch_day = epoch_day;
    summary.heartbeats_expected = expected;
    summary.heartbeats_received = received;
    summary.avg_uptime = avg_uptime;
    summary.avg_sync_lag = avg_sync_lag;
    summary.services_downtime_minutes = services_downtime_minutes;
    summary.score_delta = score_delta;
    summary.bump = ctx.bumps.epoch_summary;

    Ok(())
}
