# SolaxyEasyNode

One-line installer for a complete Solaxy node: **SVM Rollup + Celestia Light Node + PostgreSQL + Web Dashboard**.

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/USER/SolaxyEasyNode/main/install.sh | bash
```

> Replace `USER` with the GitHub username hosting this repo.

## What Gets Installed

| Component | Description |
|---|---|
| **svm-rollup** | Solaxy SVM Rollup full node |
| **Celestia Light Node** | DA layer light node (built from source) |
| **PostgreSQL** | Database for blocks, transactions, accounts |
| **Dashboard** | Web UI at `http://<LAN_IP>:5555` |

## After Installation

- **Dashboard**: `http://<your-ip>:5555`
- **Config**: `~/svm-rollup/config.toml` (also editable from the dashboard Settings panel)
- **Logs**: `journalctl -u solaxy-node -f`

### Service Management

```bash
sudo systemctl status solaxy-node
sudo systemctl restart celestia-light
sudo systemctl stop solaxy-dashboard
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
~/svm-rollup/           # Rollup binary, config, genesis, data
~/dashboard/            # Flask dashboard (app.py + templates)
~/.celestia-light/      # Celestia node store
```
