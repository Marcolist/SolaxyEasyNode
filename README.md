# SolaxyEasyNode

One-line installer for a complete Solaxy node: **SVM Rollup + Celestia Node + PostgreSQL + Web Dashboard**.

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
| **Celestia Node** | DA layer node — light or full (see below) |
| **PostgreSQL** | Database for blocks, transactions, accounts |
| **Go** | Required to build Celestia from source |
| **Dashboard** | Web UI at `http://<LAN_IP>:5555` |

### Celestia Light vs Full Node

The installer automatically detects whether the official Solaxy state export is recent enough for a Celestia Light Node (~8 days of block availability).

| | Light Node | Full Node (auto-fallback) |
|---|---|---|
| **When** | State export is fresh | State export is older than ~6 days |
| **Disk** | ~2 GB | ~20-50 GB (with 7-day pruning) |
| **RAM** | ~2 GB | ~4-8 GB |
| **Sync** | Minutes | Hours (catches up from genesis) |

The Full Node uses a 7-day pruning window to keep disk usage low — it syncs all historical blocks but only retains the last 7 days.

## After Installation

- **Dashboard**: `http://<your-ip>:5555`
- **RPC Endpoint**: `http://127.0.0.1:8899`
- **Config**: `~/svm-rollup/config.toml` (also editable from the dashboard Settings panel)
- **Logs**: `journalctl -u solaxy-node -f`

### Service Management

```bash
# Status (use celestia-full instead of celestia-light if in full mode)
sudo systemctl status solaxy-node celestia-light solaxy-dashboard

# Restart
sudo systemctl restart solaxy-node

# Logs (follow)
journalctl -u solaxy-node -f
journalctl -u celestia-light -f   # or celestia-full
```

Or use the **Settings** panel in the dashboard to start/stop/restart services.

## Dashboard Features

- Real-time sync progress for Solaxy and Celestia
- PostgreSQL stats (blocks, transactions, accounts)
- Server resource monitoring (CPU, memory, disk, network)
- Node identity and wallet info
- Reward model & node roles reference
- **Settings panel**: Edit all `config.toml` values and manage services from the UI

## Requirements

- Ubuntu 22.04 / 24.04 (or Debian-based)
- 4+ CPU cores, 8+ GB RAM (Full Node mode may need more), 100+ GB SSD
- Internet connection

## File Structure

```
~/svm-rollup/
├── svm-rollup              # Node binary
├── config.toml             # Node configuration
├── node-wallet.json        # Solana keypair
├── genesis/                # Genesis state
└── data/                   # Chain data (grows over time)

~/dashboard/
├── app.py                  # Flask dashboard
└── templates/
    └── index.html

~/.celestia-light/          # Celestia node store & keys (light mode)
~/.celestia-full/           # Celestia node store & keys (full mode)
```

## Systemd Services

| Service | Description |
|---|---|
| `celestia-light` or `celestia-full` | Celestia node (mode chosen automatically) |
| `solaxy-node` | SVM rollup node (depends on Celestia + PostgreSQL) |
| `solaxy-dashboard` | Web dashboard on port 5555 |

## Troubleshooting

```bash
# Check if all services are running
sudo systemctl status solaxy-node celestia-light solaxy-dashboard postgresql  # or celestia-full

# View recent errors
journalctl -u solaxy-node --since "10 min ago" --no-pager

# Restart everything
sudo systemctl restart celestia-light solaxy-node solaxy-dashboard  # or celestia-full
```
