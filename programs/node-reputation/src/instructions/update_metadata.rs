use anchor_lang::prelude::*;

use crate::errors::ReputationError;
use crate::state::*;

#[derive(Accounts)]
pub struct UpdateMetadata<'info> {
    pub operator: Signer<'info>,

    #[account(
        mut,
        seeds = [SEED_NODE, operator.key().as_ref()],
        bump = node.bump,
    )]
    pub node: Account<'info, NodeRegistry>,
}

pub fn handler(ctx: Context<UpdateMetadata>, metadata_uri: String) -> Result<()> {
    require!(
        metadata_uri.len() <= MAX_METADATA_URI_LEN,
        ReputationError::MetadataUriTooLong
    );
    ctx.accounts.node.metadata_uri = metadata_uri;
    Ok(())
}
