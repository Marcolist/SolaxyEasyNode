use crate::state::{HeartbeatBuffer, HeartbeatEntry, NodeRegistry, HEARTBEAT_RING_SIZE};

/// Minimum interval between heartbeats (5 minutes in seconds).
pub const MIN_HEARTBEAT_INTERVAL: i64 = 300;

/// Number of services tracked in the bitfield.
const NUM_SERVICES: u32 = 4;

/// Calculate the reputation score (0–10000) from the heartbeat ring buffer.
///
/// Weights:
///   40%  Uptime consistency   (heartbeats received vs expected)
///   25%  Sync quality         (how close to network max block height)
///   20%  Service health       (all 4 services running)
///   10%  Longevity            (days active, max 90 days)
///    5%  Peer connectivity    (peer_count, max 20)
pub fn calculate_reputation(
    node: &NodeRegistry,
    buffer: &HeartbeatBuffer,
    network_max_height: u64,
    now: i64,
) -> u16 {
    let valid: Vec<&HeartbeatEntry> = buffer
        .entries
        .iter()
        .filter(|e| e.timestamp > 0)
        .collect();

    if valid.is_empty() {
        return 0;
    }

    // ── 1. Uptime consistency (40%) ─────────────────────────────────────
    let expected = HEARTBEAT_RING_SIZE as u64;
    let received = valid.len() as u64;
    let uptime_score = ((received * 10000) / expected).min(10000) as u64;

    // ── 2. Sync quality (25%) ───────────────────────────────────────────
    let sync_score = if network_max_height == 0 {
        10000u64
    } else {
        let latest_height = valid
            .iter()
            .map(|e| e.solaxy_block_height)
            .max()
            .unwrap_or(0);
        if latest_height >= network_max_height {
            10000u64
        } else {
            let lag = network_max_height - latest_height;
            if lag > 100 {
                0u64
            } else {
                ((100 - lag) * 100) as u64
            }
        }
    };

    // ── 3. Service health (20%) ─────────────────────────────────────────
    let total_services_up: u64 = valid
        .iter()
        .map(|e| e.services_healthy.count_ones() as u64)
        .sum();
    let max_services = valid.len() as u64 * NUM_SERVICES as u64;
    let health_score = if max_services == 0 {
        0u64
    } else {
        (total_services_up * 10000) / max_services
    };

    // ── 4. Longevity (10%) ──────────────────────────────────────────────
    let days_active = if now > node.registered_at {
        ((now - node.registered_at) / 86400) as u64
    } else {
        0
    };
    let longevity_score = (days_active * 10000 / 90).min(10000);

    // ── 5. Peer connectivity (5%) ───────────────────────────────────────
    let avg_peers = if valid.is_empty() {
        0u64
    } else {
        valid.iter().map(|e| e.peer_count as u64).sum::<u64>() / valid.len() as u64
    };
    let peer_score = (avg_peers * 10000 / 20).min(10000);

    // ── Weighted sum ────────────────────────────────────────────────────
    let score = (uptime_score * 40
        + sync_score * 25
        + health_score * 20
        + longevity_score * 10
        + peer_score * 5)
        / 100;

    score.min(10000) as u16
}
