#!/usr/bin/env python3
"""
SolaxyEasyNode Reputation Relayer

Periodically reads node metrics from the local dashboard API and submits
heartbeat transactions to the on-chain Node Reputation program.

Usage:
    python3 reputation_relayer.py [--interval 600] [--dashboard http://127.0.0.1:5555]

Environment:
    SOLAXY_RPC_URL       RPC endpoint for Solaxy L2 (default: https://mainnet.rpc.solaxy.io)
    REPUTATION_PROGRAM   Program ID of the deployed node-reputation program
    NODE_WALLET_PATH     Path to the node operator keypair (default: ~/svm-rollup/node-wallet.json)
    DASHBOARD_PASSWORD   Dashboard password for API auth (optional if no password set)
"""

import argparse
import json
import logging
import os
import struct
import sys
import time
from pathlib import Path

import requests

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import ID as SYSTEM_PROGRAM_ID
    from solders.transaction import Transaction
    from solders.instruction import Instruction, AccountMeta
    from solders.hash import Hash
    from solders.commitment_config import CommitmentLevel
    from solana.rpc.api import Client
    from solana.rpc.types import TxOpts

    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reputation-relayer")

# ── Constants ────────────────────────────────────────────────────────────────

SEED_NODE = b"node"
SEED_HEARTBEAT = b"heartbeat"
SEED_NETWORK = b"network-stats"

# Anchor instruction discriminators (first 8 bytes of sha256("global:<name>"))
# Precomputed for efficiency; regenerate with anchor if program changes.
DISC_SUBMIT_HEARTBEAT = bytes([0x29, 0x38, 0x9D, 0x78, 0xC2, 0x2D, 0x6A, 0x41])

DEFAULT_RPC = "https://mainnet.rpc.solaxy.io"
DEFAULT_WALLET = os.path.expanduser("~/svm-rollup/node-wallet.json")
DEFAULT_DASHBOARD = "http://127.0.0.1:5555"
DEFAULT_INTERVAL = 600  # 10 minutes


# ── Helpers ──────────────────────────────────────────────────────────────────


def load_keypair(path: str) -> "Keypair":
    """Load a Solana keypair from a JSON file (standard solana-keygen format)."""
    with open(path) as f:
        secret = json.load(f)
    return Keypair.from_bytes(bytes(secret[:64]))


def find_pda(seeds: list[bytes], program_id: "Pubkey") -> tuple["Pubkey", int]:
    """Derive a PDA address."""
    return Pubkey.find_program_address(seeds, program_id)


def encode_heartbeat_data(
    solaxy_block_height: int,
    celestia_das_height: int,
    services_healthy: int,
    uptime_pct: int,
    cpu_usage: int,
    peer_count: int,
    attested_height: int,
) -> bytes:
    """Encode heartbeat instruction data (Anchor borsh format)."""
    return (
        DISC_SUBMIT_HEARTBEAT
        + struct.pack("<Q", solaxy_block_height)
        + struct.pack("<Q", celestia_das_height)
        + struct.pack("<B", services_healthy)
        + struct.pack("<H", uptime_pct)
        + struct.pack("<B", cpu_usage)
        + struct.pack("<H", peer_count)
        + struct.pack("<Q", attested_height)
    )


# ── Dashboard API Client ────────────────────────────────────────────────────


class DashboardClient:
    """Fetches node metrics from the local EasyNode dashboard."""

    def __init__(self, base_url: str, password: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.timeout = 10
        if password:
            self._login(password)

    def _login(self, password: str):
        resp = self.session.post(
            f"{self.base_url}/api/login",
            json={"password": password},
        )
        if resp.status_code == 200:
            log.info("Dashboard auth successful")
        else:
            log.warning("Dashboard auth failed (%s), proceeding without auth", resp.status_code)

    def get_stats(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/stats")
        resp.raise_for_status()
        return resp.json()

    def get_uptime(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/uptime")
        resp.raise_for_status()
        return resp.json()

    def get_attester_info(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/attester-info")
        resp.raise_for_status()
        return resp.json()


def parse_services_bitfield(stats: dict) -> int:
    """Build a 4-bit service health bitfield from /api/stats response."""
    bits = 0
    if stats.get("solaxy", {}).get("service", {}).get("active"):
        bits |= 0x01
    if stats.get("celestia", {}).get("service", {}).get("active"):
        bits |= 0x02
    if stats.get("postgresql", {}).get("service", {}).get("active"):
        bits |= 0x04
    rpc = stats.get("rpc", {}).get("local", {})
    if rpc.get("reachable") or rpc.get("block_height", 0) > 0:
        bits |= 0x08
    return bits


def parse_uptime_pct(uptime: dict) -> int:
    """Extract average uptime percentage from /api/uptime, as 0–10000."""
    values = []
    for svc in ("solaxy-node", "celestia", "postgresql"):
        svc_data = uptime.get(svc, {})
        pct = svc_data.get("uptime_pct", 0)
        values.append(pct)
    if not values:
        return 0
    avg = sum(values) / len(values)
    return min(int(avg * 100), 10000)


# ── Main Loop ────────────────────────────────────────────────────────────────


def run_relayer(args):
    if not HAS_SOLANA:
        log.error(
            "solders/solana-py not installed. Run: pip install solana solders"
        )
        sys.exit(1)

    program_id = Pubkey.from_string(args.program_id)
    keypair = load_keypair(args.wallet)
    operator_pubkey = keypair.pubkey()
    client = Client(args.rpc_url)
    dashboard = DashboardClient(args.dashboard, args.dashboard_password)

    # Derive PDAs
    node_pda, _ = find_pda([SEED_NODE, bytes(operator_pubkey)], program_id)
    heartbeat_pda, _ = find_pda([SEED_HEARTBEAT, bytes(operator_pubkey)], program_id)
    network_pda, _ = find_pda([SEED_NETWORK], program_id)

    log.info("Reputation Relayer started")
    log.info("  Operator:   %s", operator_pubkey)
    log.info("  Node PDA:   %s", node_pda)
    log.info("  Program:    %s", program_id)
    log.info("  RPC:        %s", args.rpc_url)
    log.info("  Interval:   %ds", args.interval)

    while True:
        try:
            # 1. Fetch metrics from dashboard
            stats = dashboard.get_stats()
            uptime = dashboard.get_uptime()
            attester = dashboard.get_attester_info()

            # 2. Extract values
            solaxy_sync = stats.get("solaxy", {}).get("sync", {})
            solaxy_block_height = solaxy_sync.get("slot", 0) or solaxy_sync.get("block_height", 0)

            celestia_sync = stats.get("celestia", {}).get("sync", {})
            celestia_das_height = celestia_sync.get("das_latest", 0) or celestia_sync.get("height", 0)

            services_healthy = parse_services_bitfield(stats)
            uptime_pct = parse_uptime_pct(uptime)

            system = stats.get("system", {})
            cpu_usage = min(int(system.get("cpu_pct", 0)), 100)

            p2p = stats.get("celestia", {}).get("p2p", {})
            peer_count = min(p2p.get("peers", 0), 65535)

            attested_height = attester.get("max_attested_height", 0) or 0

            # 3. Build and send heartbeat transaction
            ix_data = encode_heartbeat_data(
                solaxy_block_height=solaxy_block_height,
                celestia_das_height=celestia_das_height,
                services_healthy=services_healthy,
                uptime_pct=uptime_pct,
                cpu_usage=cpu_usage,
                peer_count=peer_count,
                attested_height=attested_height,
            )

            accounts = [
                AccountMeta(pubkey=operator_pubkey, is_signer=True, is_writable=False),
                AccountMeta(pubkey=node_pda, is_signer=False, is_writable=True),
                AccountMeta(pubkey=heartbeat_pda, is_signer=False, is_writable=True),
                AccountMeta(pubkey=network_pda, is_signer=False, is_writable=True),
                AccountMeta(pubkey=operator_pubkey, is_signer=False, is_writable=False),
            ]

            ix = Instruction(program_id, ix_data, accounts)
            blockhash_resp = client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash

            tx = Transaction.new_signed_with_payer(
                [ix], operator_pubkey, [keypair], blockhash
            )

            result = client.send_transaction(
                tx,
                opts=TxOpts(skip_preflight=False, preflight_commitment=CommitmentLevel.Confirmed),
            )

            sig = result.value
            log.info(
                "Heartbeat sent | block=%d das=%d services=0x%02X uptime=%d score=? | tx=%s",
                solaxy_block_height,
                celestia_das_height,
                services_healthy,
                uptime_pct,
                sig,
            )

        except requests.RequestException as e:
            log.warning("Dashboard API error: %s", e)
        except Exception as e:
            log.error("Heartbeat failed: %s", e, exc_info=True)

        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="SolaxyEasyNode Reputation Relayer")
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("SOLAXY_RPC_URL", DEFAULT_RPC),
        help="Solaxy L2 RPC endpoint",
    )
    parser.add_argument(
        "--program-id",
        default=os.environ.get("REPUTATION_PROGRAM", "RepNodE1111111111111111111111111111111111111"),
        help="Node Reputation program ID",
    )
    parser.add_argument(
        "--wallet",
        default=os.environ.get("NODE_WALLET_PATH", DEFAULT_WALLET),
        help="Path to operator keypair JSON",
    )
    parser.add_argument(
        "--dashboard",
        default=os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD),
        help="Dashboard base URL",
    )
    parser.add_argument(
        "--dashboard-password",
        default=os.environ.get("DASHBOARD_PASSWORD"),
        help="Dashboard password (if set)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("HEARTBEAT_INTERVAL", DEFAULT_INTERVAL)),
        help="Seconds between heartbeats (default: 600)",
    )
    args = parser.parse_args()
    run_relayer(args)


if __name__ == "__main__":
    main()
