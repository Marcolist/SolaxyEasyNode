#!/usr/bin/env bash
# =============================================================================
# SolaxyEasyNode — One-Line Installer
# Sets up: SVM Rollup + Celestia Light Node + PostgreSQL + Dashboard
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/USER/SolaxyEasyNode/main/install.sh | bash
# =============================================================================
set -euo pipefail

REPO_URL="https://raw.githubusercontent.com/USER/SolaxyEasyNode/main"
CELESTIA_CORE_IP="rpc.celestia.pops.one"
CELESTIA_CORE_PORT="9090"
CELESTIA_GRPC="http://${CELESTIA_CORE_IP}:${CELESTIA_CORE_PORT}"
CELESTIA_REPO="https://github.com/celestiaorg/celestia-node.git"
CELESTIA_VERSION="v0.22.2"
GO_VERSION="1.23.4"

USER_NAME="$(whoami)"
USER_HOME="$HOME"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "SolaxyEasyNode Installer starting..."
log "User: $USER_NAME | Home: $USER_HOME"

if [[ $EUID -eq 0 ]]; then
    warn "Running as root. Services will run as root."
fi

# ---------------------------------------------------------------------------
# Step 1: System dependencies
# ---------------------------------------------------------------------------
log "Installing system dependencies..."
sudo apt update -qq
sudo apt install -y build-essential cmake pkg-config libudev-dev \
    postgresql python3 python3-pip libpq-dev curl tar git jq

# ---------------------------------------------------------------------------
# Step 2: Download svm-rollup
# ---------------------------------------------------------------------------
log "Downloading svm-rollup..."
mkdir -p "$USER_HOME/svm-rollup"
cd "$USER_HOME/svm-rollup"

if [[ ! -f "$USER_HOME/svm-rollup/svm-rollup" ]]; then
    curl -LO https://download.solaxy.io/solaxy/svm-rollup.tar.gz
    tar xzf svm-rollup.tar.gz
    rm -f svm-rollup.tar.gz
    chmod +x svm-rollup
    log "svm-rollup extracted."
else
    warn "svm-rollup binary already exists, skipping download."
fi

# ---------------------------------------------------------------------------
# Step 3: Download genesis
# ---------------------------------------------------------------------------
log "Downloading genesis state..."
mkdir -p "$USER_HOME/svm-rollup/genesis"

if [[ ! -f "$USER_HOME/svm-rollup/genesis/state_export.svmd" ]]; then
    curl -LO https://download.solaxy.io/solaxy/state_export.svmd
    mv state_export.svmd "$USER_HOME/svm-rollup/genesis/"
    log "Genesis state downloaded."
else
    warn "Genesis state already exists, skipping download."
fi

# ---------------------------------------------------------------------------
# Step 4: Install Go (needed for Celestia)
# ---------------------------------------------------------------------------
if ! command -v go &>/dev/null; then
    log "Installing Go ${GO_VERSION}..."
    curl -LO "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
    sudo rm -rf /usr/local/go
    sudo tar -C /usr/local -xzf "go${GO_VERSION}.linux-amd64.tar.gz"
    rm -f "go${GO_VERSION}.linux-amd64.tar.gz"
    export PATH=$PATH:/usr/local/go/bin:$USER_HOME/go/bin
    # Persist PATH
    if ! grep -q '/usr/local/go/bin' "$USER_HOME/.profile" 2>/dev/null; then
        echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> "$USER_HOME/.profile"
    fi
    log "Go installed."
else
    log "Go already installed: $(go version)"
    export PATH=$PATH:/usr/local/go/bin:$USER_HOME/go/bin
fi

# ---------------------------------------------------------------------------
# Step 5: Install Celestia Light Node
# ---------------------------------------------------------------------------
if ! command -v celestia &>/dev/null; then
    log "Building Celestia light node from source..."
    cd /tmp
    rm -rf celestia-node
    git clone --depth 1 --branch "$CELESTIA_VERSION" "$CELESTIA_REPO"
    cd celestia-node
    make build
    sudo make install
    cd "$USER_HOME"
    rm -rf /tmp/celestia-node
    log "Celestia installed: $(celestia version)"
else
    log "Celestia already installed: $(celestia version)"
fi

# ---------------------------------------------------------------------------
# Step 6: Init Celestia light node & extract auth token
# ---------------------------------------------------------------------------
CELESTIA_STORE="$USER_HOME/.celestia-light"

if [[ ! -d "$CELESTIA_STORE/keys" ]]; then
    log "Initializing Celestia light node..."
    INIT_OUTPUT=$(celestia light init 2>&1)
    echo "$INIT_OUTPUT"
    log "Celestia light node initialized."
else
    warn "Celestia light node already initialized."
fi

# Extract auth token
log "Extracting Celestia auth token..."
CELESTIA_AUTH_TOKEN=$(celestia light auth admin --node.store "$CELESTIA_STORE" 2>/dev/null || true)

if [[ -z "$CELESTIA_AUTH_TOKEN" ]]; then
    # Try to start celestia briefly to generate the token
    warn "Could not get auth token yet. Will start Celestia to generate it..."
    celestia light start --core.ip "$CELESTIA_CORE_IP" --core.port "$CELESTIA_CORE_PORT" \
        --keyring.keyname my_celes_key &
    CELESTIA_PID=$!
    sleep 10
    CELESTIA_AUTH_TOKEN=$(celestia light auth admin --node.store "$CELESTIA_STORE" 2>/dev/null || true)
    kill $CELESTIA_PID 2>/dev/null || true
    wait $CELESTIA_PID 2>/dev/null || true
fi

if [[ -z "$CELESTIA_AUTH_TOKEN" ]]; then
    err "Failed to extract Celestia auth token. Please check Celestia installation."
fi
log "Auth token extracted."

# ---------------------------------------------------------------------------
# Step 7: Extract signer private key from Celestia keyring
# ---------------------------------------------------------------------------
log "Extracting signer private key..."
SIGNER_KEY=""

# Try to export from keyring
KEY_EXPORT=$(celestia-appd keys export my_celes_key --unarmored-hex --unsafe \
    --keyring-backend test --keyring-dir "$CELESTIA_STORE/keys" 2>/dev/null || true)

if [[ -n "$KEY_EXPORT" && ${#KEY_EXPORT} -ge 64 ]]; then
    SIGNER_KEY="$KEY_EXPORT"
else
    # Fallback: try to read from keyring file directly
    KEYRING_FILE="$CELESTIA_STORE/keys/keyring-test/my_celes_key.info"
    if [[ -f "$KEYRING_FILE" ]]; then
        # Extract hex key from keyring
        SIGNER_KEY=$(cat "$KEYRING_FILE" | od -A n -t x1 | tr -d ' \n' | tail -c 64)
    fi
fi

if [[ -z "$SIGNER_KEY" ]]; then
    warn "Could not auto-extract signer key. Using placeholder — update config.toml manually."
    SIGNER_KEY="REPLACE_WITH_YOUR_SIGNER_PRIVATE_KEY"
fi

# ---------------------------------------------------------------------------
# Step 8: Setup PostgreSQL
# ---------------------------------------------------------------------------
log "Setting up PostgreSQL..."
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Create database and user
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='postgres'" | grep -q 1 || true
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'secret';" 2>/dev/null || true
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='svm'" | grep -q 1 || \
    sudo -u postgres createdb svm

# Create tables
sudo -u postgres psql -d svm -c "
CREATE TABLE IF NOT EXISTS blocks (
    slot BIGINT PRIMARY KEY,
    hash TEXT,
    parent_slot BIGINT,
    block_time BIGINT,
    block_height BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS transactions (
    signature TEXT PRIMARY KEY,
    slot BIGINT,
    err BOOLEAN DEFAULT FALSE,
    fee BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS accounts (
    pubkey TEXT PRIMARY KEY,
    lamports BIGINT DEFAULT 0,
    owner TEXT,
    executable BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT NOW()
);
" 2>/dev/null || true

log "PostgreSQL configured (database: svm)."

# ---------------------------------------------------------------------------
# Step 9: Generate config.toml
# ---------------------------------------------------------------------------
log "Generating config.toml..."
cd "$USER_HOME/svm-rollup"

# Download template
curl -sSL "${REPO_URL}/config.toml.template" -o /tmp/config.toml.template 2>/dev/null || true

# If curl failed (no internet or repo not public yet), use embedded template
if [[ ! -f /tmp/config.toml.template ]]; then
cat > /tmp/config.toml.template << 'TMPL'
[da]
rpc_url = "ws://127.0.0.1:26658"
rpc_auth_token = "%%RPC_AUTH_TOKEN%%"
grpc_url = "%%GRPC_URL%%"
signer_private_key = "%%SIGNER_PRIVATE_KEY%%"

[storage]
path = "data"
pruner_versions_to_keep = 50
user_commit_concurrency = 8
kernel_commit_concurrency = 4
user_hashtable_buckets = 16000000
user_page_cache_size = 4096
kernel_page_cache_size = 1024

[runner]
da_polling_interval_ms = 50
concurrent_sync_tasks = 20
pre_fetched_blocks_capacity = 100
save_tx_bodies = true

[runner.http_config]
bind_host = "127.0.0.1"
bind_port = 8899

[monitoring]
telegraf_address = "127.0.0.1:8094"

[proof_manager]
aggregated_proof_block_jump = 1
prover_address = "HjjEhif8MU9DtnXtZc5hkBu9XLAkAYe1qwzhDoxbcECv"
max_number_of_transitions_in_db = 100
max_number_of_transitions_in_memory = 30

[sequencer]
rollup_address = "HjjEhif8MU9DtnXtZc5hkBu9XLAkAYe1qwzhDoxbcECv"
max_allowed_node_distance_behind = 5
max_concurrent_blobs = 128
max_batch_size_bytes = 1048576
blob_processing_timeout_secs = 120

[sequencer.standard]
TMPL
fi

# Replace placeholders
sed -e "s|%%RPC_AUTH_TOKEN%%|${CELESTIA_AUTH_TOKEN}|g" \
    -e "s|%%GRPC_URL%%|${CELESTIA_GRPC}|g" \
    -e "s|%%SIGNER_PRIVATE_KEY%%|${SIGNER_KEY}|g" \
    /tmp/config.toml.template > "$USER_HOME/svm-rollup/config.toml"

rm -f /tmp/config.toml.template
log "config.toml generated."

# ---------------------------------------------------------------------------
# Step 10: Install Python dependencies
# ---------------------------------------------------------------------------
log "Installing Python dependencies..."
pip3 install --break-system-packages flask psycopg2-binary requests 2>/dev/null || \
    pip3 install flask psycopg2-binary requests

# ---------------------------------------------------------------------------
# Step 11: Setup Dashboard
# ---------------------------------------------------------------------------
log "Setting up dashboard..."
mkdir -p "$USER_HOME/dashboard/templates"

curl -sSL "${REPO_URL}/dashboard/app.py" -o "$USER_HOME/dashboard/app.py" 2>/dev/null || true
curl -sSL "${REPO_URL}/dashboard/templates/index.html" -o "$USER_HOME/dashboard/templates/index.html" 2>/dev/null || true

if [[ ! -f "$USER_HOME/dashboard/app.py" ]]; then
    warn "Could not download dashboard files. Copy them manually from the repo."
fi

# ---------------------------------------------------------------------------
# Step 12: Generate node wallet
# ---------------------------------------------------------------------------
log "Setting up Solaxy node wallet..."
SOLANA_KEYGEN="$USER_HOME/.local/share/solana/install/active_release/bin/solana-keygen"

if [[ ! -f "$USER_HOME/svm-rollup/node-wallet.json" ]]; then
    if command -v solana-keygen &>/dev/null; then
        solana-keygen new --outfile "$USER_HOME/svm-rollup/node-wallet.json" --no-bip39-passphrase --force
    elif [[ -f "$SOLANA_KEYGEN" ]]; then
        "$SOLANA_KEYGEN" new --outfile "$USER_HOME/svm-rollup/node-wallet.json" --no-bip39-passphrase --force
    else
        warn "solana-keygen not found. Install Solana CLI to generate wallet, or copy an existing wallet."
    fi
else
    warn "Node wallet already exists."
fi

# ---------------------------------------------------------------------------
# Step 13: Install systemd services
# ---------------------------------------------------------------------------
log "Installing systemd services..."

install_service() {
    local name="$1"
    local url="${REPO_URL}/services/${name}"
    local tmp="/tmp/${name}"

    curl -sSL "$url" -o "$tmp" 2>/dev/null || true

    # If download failed, generate from embedded template
    if [[ ! -f "$tmp" || ! -s "$tmp" ]]; then
        warn "Could not download ${name}, using embedded version."
        case "$name" in
            solaxy-node.service)
cat > "$tmp" << EOF
[Unit]
Description=Solaxy SVM Rollup Node
After=network-online.target postgresql.service celestia-light.service
Wants=network-online.target
Requires=postgresql.service celestia-light.service

[Service]
User=${USER_NAME}
Type=simple
WorkingDirectory=${USER_HOME}/svm-rollup
Environment=SOV_PROVER_MODE=skip
ExecStart=${USER_HOME}/svm-rollup/svm-rollup --da-layer celestia --rollup-config-path config.toml --genesis-config-dir genesis
Restart=on-failure
RestartSec=15
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
            ;;
            celestia-light.service)
cat > "$tmp" << EOF
[Unit]
Description=Celestia Light Node
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
Type=simple
ExecStart=/usr/local/bin/celestia light start --core.ip ${CELESTIA_CORE_IP} --core.port ${CELESTIA_CORE_PORT} --keyring.keyname my_celes_key
Restart=on-failure
RestartSec=10
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
            ;;
            solaxy-dashboard.service)
cat > "$tmp" << EOF
[Unit]
Description=Solaxy Node Dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
Type=simple
WorkingDirectory=${USER_HOME}/dashboard
ExecStart=/usr/bin/python3 ${USER_HOME}/dashboard/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
            ;;
        esac
    fi

    # Replace placeholders
    sed -i "s|%%USER%%|${USER_NAME}|g; s|%%HOME%%|${USER_HOME}|g" "$tmp"
    sudo cp "$tmp" "/etc/systemd/system/${name}"
    rm -f "$tmp"
}

install_service "solaxy-node.service"
install_service "celestia-light.service"
install_service "solaxy-dashboard.service"

sudo systemctl daemon-reload
sudo systemctl enable celestia-light solaxy-node solaxy-dashboard
sudo systemctl start celestia-light
log "Waiting for Celestia to initialize..."
sleep 15
sudo systemctl start solaxy-node
sudo systemctl start solaxy-dashboard

# ---------------------------------------------------------------------------
# Step 14: Print summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${GREEN}  SolaxyEasyNode Installation Complete!${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

LAN_IP=$(hostname -I | awk '{print $1}')

echo -e "  Dashboard:    ${CYAN}http://${LAN_IP}:5555${NC}"
echo -e "  LAN IP:       ${CYAN}${LAN_IP}${NC}"
echo ""
echo -e "  Service Status:"
echo -e "    celestia-light:    $(systemctl is-active celestia-light.service)"
echo -e "    solaxy-node:       $(systemctl is-active solaxy-node.service)"
echo -e "    solaxy-dashboard:  $(systemctl is-active solaxy-dashboard.service)"
echo -e "    postgresql:        $(systemctl is-active postgresql.service)"
echo ""
echo -e "  Config:       ${CYAN}${USER_HOME}/svm-rollup/config.toml${NC}"
echo -e "  Node Wallet:  ${CYAN}${USER_HOME}/svm-rollup/node-wallet.json${NC}"
echo -e "  Logs:         journalctl -u solaxy-node -f"
echo ""
echo -e "${CYAN}============================================================${NC}"
