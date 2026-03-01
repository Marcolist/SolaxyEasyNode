# SolaxyEasyNode

One-line installer for a complete Solaxy node: **SVM Rollup + Celestia Light Node + PostgreSQL + Web Dashboard**.

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

## What Gets Installed

| Component | Description |
|---|---|
| **svm-rollup** | Solaxy SVM Rollup full node |
| **Celestia Light Node** | DA layer light node (built from source) |
| **PostgreSQL** | Database for blocks, transactions, accounts |
| **Go** | Required to build Celestia from source |
| **Dashboard** | Web UI at `http://<LAN_IP>:5555` |

## After Installation

- **Dashboard**: `http://<your-ip>:5555`
- **RPC Endpoint**: `http://127.0.0.1:8899`
- **Config**: `~/svm-rollup/config.toml` (also editable from the dashboard Settings panel)
- **Logs**: `journalctl -u solaxy-node -f`

### Service Management

```bash
# Status
sudo systemctl status solaxy-node celestia-light solaxy-dashboard

# Restart
sudo systemctl restart solaxy-node

# Logs (follow)
journalctl -u solaxy-node -f
journalctl -u celestia-light -f
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
- 4+ CPU cores, 8+ GB RAM, 100+ GB SSD
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

~/.celestia-light/          # Celestia node store & keys
```

## Systemd Services

| Service | Description |
|---|---|
| `celestia-light` | Celestia light node (starts first) |
| `solaxy-node` | SVM rollup node (depends on Celestia + PostgreSQL) |
| `solaxy-dashboard` | Web dashboard on port 5555 |

## Troubleshooting

```bash
# Check if all services are running
sudo systemctl status solaxy-node celestia-light solaxy-dashboard postgresql

# View recent errors
journalctl -u solaxy-node --since "10 min ago" --no-pager

# Restart everything
sudo systemctl restart celestia-light solaxy-node solaxy-dashboard
```
