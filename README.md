# SolaxyEasyNode

One-line installer for a complete Solaxy node: **SVM Rollup + Celestia Bridge Node + PostgreSQL + Web Dashboard**.

```
                    ___
                ___/ _ \___
            ___/   (_)   \___
         __/                 \__
       _/    S O L A X Y        \_
     _/ _________________________ \_
    /__|_________________________|__\
   ///                             \\\
  ///═══════════════════════════════\\\
  \\\           ▀▀▀▀▀▀▀            ///
   \\\___________________________///
    \_____________________________/
        \_____________________/
           \_______________/
              \  \   /  /
               \  \ /  /
                \_   _/
                  \_/
```

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/Marcolist/SolaxyEasyNode/main/install.sh | bash
```

The installer shows progress bars for all downloads and extractions.

## Why this exists

Running a Solaxy node currently requires multiple manual steps:
- Celestia build
- Rollup config
- Service wiring
- Monitoring setup

SolaxyEasyNode reduces setup time and provides a production-ready dashboard + Telegram alerts.

## Screenshots

### Node Dashboard
![Dashboard](screenshots/dashboard.png)

### Telegram Bot
![Telegram Bot](screenshots/telegrambot.png)

### Node Map
![Network Map](screenshots/map.png)

## What Gets Installed

| Component | Description |
|---|---|
| **svm-rollup** | Solaxy SVM Rollup full node |
| **Celestia Bridge Node** | DA layer bridge node (Mainnet, with pruning) |
| **PostgreSQL** | Database for blocks, transactions, accounts |
| **Go 1.26.1** | Required to build Celestia v0.29.1-mocha from source |
| **Dashboard** | Web UI at `http://<LAN_IP>:5555` |

### Celestia Bridge Node

The installer uses a Celestia Bridge Node in pruning mode. The `SyncFromHeight` is set automatically to the genesis DA height from `chain_state_zk.json`, so no unnecessary blocks are synced.

| | Bridge Node |
|---|---|
| **Network** | Celestia Mainnet (`--p2p.network celestia`) |
| **Disk** | ~20-50 GB (with pruning window) |
| **RAM** | ~4-8 GB |
| **Sync** | Hours (from genesis DA height) |
| **Pruning** | Dynamic — covers genesis-to-head + 48h buffer (min 720h) |
| **RPC Auth** | Skipped (`--rpc.skip-auth`) for stable rollup connection |

## Wallet, Rewards & Bond

Each node generates a Solana keypair (`~/svm-rollup/node-wallet.json`) during installation. This wallet is used for:

- **Prover rewards** — configured as `prover_address` in `config.toml`
- **Sequencer identity** — configured as `rollup_address` in `config.toml`
- **Operator incentives** — configured as `reward_address` in `genesis/operator_incentives.json`

### Bond Requirements

To participate as a sequencer or prover, the wallet must be funded **on the Solaxy rollup** (not on Solana mainnet):

| Role | Minimum Bond | Purpose |
|---|---|---|
| **Sequencer** | 10,000 SOLX | Registration bond for standard sequencer |
| **Prover (ZK)** | 200,000 SOLX | Bond for generating ZK proofs |
| **Total** | **210,000 SOLX** | Minimum to register as both |

The bond is posted automatically when the node syncs genesis with the correct wallet configured.

### Wallet Setup (New Install)

The installer automatically configures all three files with your node wallet. No manual steps needed.

### Wallet Setup (Existing Install)

If your node was installed before this update, the config may still use the default team wallet. You have two options:

**Option A — Dashboard (recommended):**
Open the dashboard and look for the **Wallet & Bond Status** panel. If a mismatch is detected, click **"Apply Wallet & Resync Now"**.

**Option B — Re-run the installer:**
```bash
curl -sSL https://raw.githubusercontent.com/Marcolist/SolaxyEasyNode/main/install.sh | bash
```
The installer detects the old wallet and offers to update + resync.

> **Warning:** Changing the wallet requires a full data wipe and resync from genesis. The node will re-import the 20 GB state export and sync all blocks from scratch. This can take several hours depending on your hardware.

### Verify Wallet Configuration

```bash
# Check which wallet is configured
grep -E "prover_address|rollup_address" ~/svm-rollup/config.toml
cat ~/svm-rollup/genesis/operator_incentives.json

# Check your node wallet address
python3 -c "
import json
alphabet = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def b58(data):
    n = int.from_bytes(data, 'big'); r = b''
    while n > 0: n, m = divmod(n, 58); r = alphabet[m:m+1] + r
    return r.decode()
print(b58(bytes(json.load(open('$HOME/svm-rollup/node-wallet.json'))[32:])))
"
```

All three addresses should match your node wallet.

## After Installation

- **Dashboard**: `http://<your-ip>:5555`
- **RPC Endpoint**: `http://127.0.0.1:8899`
- **Config**: `~/svm-rollup/config.toml` (also editable from the dashboard Settings panel)
- **Node Wallet**: `~/svm-rollup/node-wallet.json`
- **Logs**: `journalctl -u solaxy-node -f`

### Service Management

```bash
# Status
sudo systemctl status solaxy-node celestia-bridge solaxy-dashboard

# Restart
sudo systemctl restart solaxy-node

# Logs (follow)
journalctl -u solaxy-node -f
journalctl -u celestia-bridge -f
```

Or use the **Settings** panel in the dashboard to start/stop/restart services.

## Dashboard Features

- Real-time sync progress for Solaxy and Celestia
- PostgreSQL stats (blocks, transactions, accounts)
- Server resource monitoring (CPU, memory, disk, network)
- Node identity and wallet info (LAN IP, hostname, public IP)
- Wallet & bond status with one-click resync
- Reward model & node roles reference
- **Settings panel**: Edit all `config.toml` values and manage services from the UI

## Network Map Integration

The dashboard sends periodic heartbeats to the [Public Validator Map](https://map.orbitnode.dev). The heartbeat payload includes:

```json
{
  "sync_status": "synced | syncing | offline",
  "uptime_seconds": 86400,
  "slot": 21093341,
  "da_height": 10259382,
  "configured_wallet": "351wxoAtyTjJV63h2gru4YrPmZWwBZRonJe5Z3Bxkt97",
  "bond_status": "bonded | unbonded | not_configured | unknown",
  "roles": ["sequencer", "prover"]
}
```

| Field | Description |
|---|---|
| `sync_status` | Node sync state (`synced`, `syncing`, `offline`) |
| `uptime_seconds` | Seconds since solaxy-node service started |
| `slot` | Current SVM slot number |
| `da_height` | Current synced DA (Celestia) block height |
| `configured_wallet` | The wallet address configured for rewards/bond |
| `bond_status` | `bonded` (sufficient SOLX for at least one role), `unbonded` (wallet configured but insufficient funds), `not_configured` (still using team wallet), `unknown` (could not check) |
| `roles` | Active roles based on balance: `sequencer` (>=10k SOLX), `prover` (>=200k SOLX) |

## Requirements

- Ubuntu 22.04 / 24.04 (or Debian-based)
- 4+ CPU cores, 8+ GB RAM, 100+ GB SSD
- Internet connection

## File Structure

```
~/svm-rollup/
├── svm-rollup              # Node binary
├── config.toml             # Node configuration
├── node-wallet.json        # Solana keypair
├── genesis/                # Genesis state
│   └── chain_state_zk.json # Contains genesis_da_height for SyncFromHeight
└── data/                   # Chain data (grows over time)

~/dashboard/
├── app.py                  # Flask dashboard
└── templates/
    └── index.html

~/.celestia-bridge/         # Celestia bridge node store & keys
```

## Systemd Services

| Service | Description |
|---|---|
| `celestia-bridge` | Celestia bridge node (Mainnet, `Restart=always`) |
| `solaxy-node` | SVM rollup node (depends on Celestia + PostgreSQL) |
| `solaxy-dashboard` | Web dashboard on port 5555 |

## Troubleshooting

```bash
# Check if all services are running
sudo systemctl status solaxy-node celestia-bridge solaxy-dashboard postgresql

# View recent errors
journalctl -u solaxy-node --since "10 min ago" --no-pager
journalctl -u celestia-bridge --since "10 min ago" --no-pager

# Restart everything
sudo systemctl restart celestia-bridge solaxy-node solaxy-dashboard

# If Celestia bridge fails to start, check core endpoint
nc -w 3 rpc.celestia.pops.one 9090 && echo "OK" || echo "NOT REACHABLE"

# If rollup can't connect to Celestia RPC, verify --rpc.skip-auth is set
grep ExecStart /etc/systemd/system/celestia-bridge.service
```
