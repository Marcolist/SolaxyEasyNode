#!/usr/bin/env bash
# =============================================================================
# SolaxyEasyNode — One-Line Installer
# Sets up: SVM Rollup + Celestia Node (light or full) + PostgreSQL + Dashboard
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Marcolist/SolaxyEasyNode/main/install.sh | bash
# =============================================================================
set -euo pipefail

REPO_URL="https://raw.githubusercontent.com/Marcolist/SolaxyEasyNode/main"
CELESTIA_CORE_IP="rpc.celestia.pops.one"
CELESTIA_CORE_PORT="9090"
CELESTIA_GRPC="http://${CELESTIA_CORE_IP}:${CELESTIA_CORE_PORT}"
CELESTIA_REPO="https://github.com/celestiaorg/celestia-node.git"
CELESTIA_VERSION="v0.28.4"
GO_VERSION="1.25.1"

CELESTIA_PRUNING_WINDOW=""  # calculated dynamically after genesis DA height is known
USE_CELESTIA_FULL=false     # set to true automatically when state export is too old for light node

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
# Helper: Check if remote file is newer than local file
# ---------------------------------------------------------------------------
# Usage: check_and_download <url> <local_path> <label>
# Returns 0 if file is ready to use, 1 on error.
check_and_download() {
    local url="$1"
    local local_path="$2"
    local label="$3"

    if [[ ! -f "$local_path" ]]; then
        log "Downloading ${label}..."
        curl -L# "$url" -o "$local_path"
        return $?
    fi

    # File exists — check if remote version is newer
    local local_epoch
    local_epoch=$(stat -c %Y "$local_path" 2>/dev/null || echo 0)

    local remote_date
    remote_date=$(curl -sI "$url" 2>/dev/null | grep -i '^last-modified:' | sed 's/^[Ll]ast-[Mm]odified: *//' | tr -d '\r')

    if [[ -z "$remote_date" ]]; then
        warn "Could not check remote version of ${label}. Using local file."
        return 0
    fi

    local remote_epoch
    remote_epoch=$(date -d "$remote_date" +%s 2>/dev/null || echo 0)

    if [[ "$remote_epoch" -gt $((local_epoch + 60)) ]]; then
        local local_date
        local_date=$(date -d "@$local_epoch" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "unknown")
        local remote_pretty
        remote_pretty=$(date -d "$remote_date" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$remote_date")

        echo ""
        warn "${label} — a newer version is available!"
        echo -e "    Local:  ${YELLOW}${local_date}${NC}"
        echo -e "    Remote: ${GREEN}${remote_pretty}${NC}"
        echo ""
        read -rp "  Download newer version? [Y/n] " answer </dev/tty
        case "${answer,,}" in
            n|no)
                log "Keeping local ${label}."
                return 0
                ;;
            *)
                log "Downloading newer ${label}..."
                rm -f "$local_path"
                curl -L# "$url" -o "$local_path"
                return $?
                ;;
        esac
    else
        log "${label} is up to date, skipping download."
        return 0
    fi
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo -e "${CYAN}"
cat << 'BANNER'
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

       ╔════════════════════════════╗
       ║   S O L A X Y  N O D E    ║
       ║     E A S Y  S E T U P    ║
       ╚════════════════════════════╝
BANNER
echo -e "${NC}"

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
    postgresql python3 python3-pip libpq-dev curl tar git jq pv

# ---------------------------------------------------------------------------
# Step 2: Download svm-rollup
# ---------------------------------------------------------------------------
log "Downloading svm-rollup..."
mkdir -p "$USER_HOME/svm-rollup"
cd "$USER_HOME/svm-rollup"

SVM_TARBALL="$USER_HOME/svm-rollup/svm-rollup.tar.gz"
SVM_URL="https://download.solaxy.io/solaxy/svm-rollup.tar.gz"

# Use binary as age reference (tarball gets deleted after extraction)
if [[ -f "$USER_HOME/svm-rollup/svm-rollup" && ! -f "$SVM_TARBALL" ]]; then
    touch -r "$USER_HOME/svm-rollup/svm-rollup" "$SVM_TARBALL" 2>/dev/null || true
fi

check_and_download "$SVM_URL" "$SVM_TARBALL" "svm-rollup"

if [[ -f "$SVM_TARBALL" && -s "$SVM_TARBALL" ]]; then
    log "Extracting svm-rollup..."
    pv "$SVM_TARBALL" | tar xzf - --strip-components=1 2>/dev/null || tar xzf "$SVM_TARBALL" --strip-components=1
    rm -f "$SVM_TARBALL"
    rm -f config.toml   # remove tar template; will be generated with correct values later
    chmod +x svm-rollup
    log "svm-rollup extracted."
fi

# ---------------------------------------------------------------------------
# Step 3: Download genesis
# ---------------------------------------------------------------------------
log "Downloading genesis state..."
mkdir -p "$USER_HOME/svm-rollup/genesis"

GENESIS_PATH="$USER_HOME/svm-rollup/genesis/state_export.svmd"
GENESIS_URL="https://download.solaxy.io/solaxy/state_export.svmd"

check_and_download "$GENESIS_URL" "$GENESIS_PATH" "genesis state (state_export.svmd)"

# Read genesis DA height from chain_state_zk.json and derive Celestia sync start
GENESIS_DA_HEIGHT=""
CHAIN_STATE_FILE="$USER_HOME/svm-rollup/genesis/chain_state_zk.json"
if [[ -f "$CHAIN_STATE_FILE" ]]; then
    GENESIS_DA_HEIGHT=$(python3 -c "import json; print(json.load(open('$CHAIN_STATE_FILE')).get('genesis_da_height', ''))" 2>/dev/null || true)
fi

if [[ -n "$GENESIS_DA_HEIGHT" && "$GENESIS_DA_HEIGHT" -gt 0 ]] 2>/dev/null; then
    # Calculate pruning window: (current_head - genesis_height) * 11s per block + 48h buffer
    CELESTIA_CURRENT_HEAD=$(curl -s "https://${CELESTIA_CORE_IP}/header" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['header']['height'])" 2>/dev/null || true)
    if [[ -n "$CELESTIA_CURRENT_HEAD" && "$CELESTIA_CURRENT_HEAD" -gt 0 ]] 2>/dev/null; then
        BLOCK_DIFF=$((CELESTIA_CURRENT_HEAD - GENESIS_DA_HEIGHT))
        PRUNING_HOURS=$(( (BLOCK_DIFF * 11 / 3600) + 48 ))
        # Minimum 720h, no maximum
        if [[ $PRUNING_HOURS -lt 720 ]]; then PRUNING_HOURS=720; fi
        CELESTIA_PRUNING_WINDOW="${PRUNING_HOURS}h0m0s"
        log "Celestia pruning window: ${PRUNING_HOURS}h (${BLOCK_DIFF} blocks since genesis + 48h buffer)"
    else
        CELESTIA_PRUNING_WINDOW="720h0m0s"
        warn "Could not fetch Celestia head height, using default pruning window (720h)"
    fi

    # Start syncing a few thousand blocks before genesis to have a margin
    CELESTIA_SYNC_FROM_HEIGHT=$((GENESIS_DA_HEIGHT - 26000))
    log "Genesis DA height: $GENESIS_DA_HEIGHT — Celestia will sync from $CELESTIA_SYNC_FROM_HEIGHT"

    # Check if the required blocks are still available on the Celestia network.
    # Our local pruning window controls what we RETAIN after sync, but for the
    # initial sync we depend on what other light/bridge nodes still have.
    # Celestia light nodes typically keep ~7-9 days of blocks.  We use a
    # conservative estimate of 8 days (~62,836 blocks at 11s/block) to decide
    # whether the needed blocks are likely still available from network peers.
    CELESTIA_NET_AVAIL_HOURS=192   # 8 days — conservative light-node availability
    ESTIMATED_TAIL=$((CELESTIA_CURRENT_HEAD - (CELESTIA_NET_AVAIL_HOURS * 3600 / 11)))
    if [[ "$CELESTIA_SYNC_FROM_HEIGHT" -lt "$ESTIMATED_TAIL" ]]; then
        echo ""
        echo -e "${YELLOW}============================================================${NC}"
        echo -e "${YELLOW}  Solaxy state export requires historical Celestia blocks${NC}"
        echo -e "${YELLOW}============================================================${NC}"
        echo ""
        echo -e "  Genesis DA Height:              ${YELLOW}${GENESIS_DA_HEIGHT}${NC}"
        echo -e "  Required sync start:            ${YELLOW}${CELESTIA_SYNC_FROM_HEIGHT}${NC}"
        echo -e "  Celestia tail (estimated, ~8d):  ${YELLOW}${ESTIMATED_TAIL}${NC}"
        echo -e "  Celestia head:                  ${CYAN}${CELESTIA_CURRENT_HEAD}${NC}"
        echo ""
        echo -e "  A Celestia Light Node only keeps ~8 days of blocks."
        echo -e "  To sync from genesis, a ${CYAN}Celestia Full Node${NC} is required."
        echo -e "  It will use pruning (7 days) to keep disk usage low."
        echo ""
        echo -e "  ${YELLOW}Note: Full Node needs ~4-8 GB RAM and ~20-50 GB disk.${NC}"
        echo -e "  Initial sync may take several hours."
        echo ""
        read -rp "  Install Celestia Full Node for sync? [Y/n] " answer </dev/tty
        case "${answer,,}" in
            n|no)
                err "Cannot continue — state export too old for light node and full node declined."
                ;;
        esac

        USE_CELESTIA_FULL=true
        CELESTIA_PRUNING_WINDOW="168h0m0s"   # 7 days — keep disk usage low
        log "Celestia Full Node mode enabled (pruning: 168h)."
    fi

    # Fetch the block hash from Celestia consensus RPC
    CELESTIA_SYNC_FROM_HASH=$(curl -s "https://${CELESTIA_CORE_IP}/header?height=${CELESTIA_SYNC_FROM_HEIGHT}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['header']['last_block_id']['hash'])" 2>/dev/null || true)

    if [[ -z "$CELESTIA_SYNC_FROM_HASH" ]]; then
        warn "Could not fetch block hash for height $CELESTIA_SYNC_FROM_HEIGHT. Celestia will sync from network head."
        CELESTIA_SYNC_FROM_HEIGHT=""
    fi
else
    warn "Could not read genesis_da_height from chain_state_zk.json."
    CELESTIA_PRUNING_WINDOW="720h0m0s"
    CELESTIA_SYNC_FROM_HEIGHT=""
    CELESTIA_SYNC_FROM_HASH=""
fi

# ---------------------------------------------------------------------------
# Step 4: Install Go (needed for Celestia)
# ---------------------------------------------------------------------------
if ! command -v go &>/dev/null; then
    log "Installing Go ${GO_VERSION}..."
    curl -L# "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o "go${GO_VERSION}.linux-amd64.tar.gz"
    sudo rm -rf /usr/local/go
    log "Extracting Go..."
    pv "go${GO_VERSION}.linux-amd64.tar.gz" | sudo tar -C /usr/local -xzf - 2>/dev/null || sudo tar -C /usr/local -xzf "go${GO_VERSION}.linux-amd64.tar.gz"
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
# Step 6: Init Celestia node & extract auth token
# ---------------------------------------------------------------------------
# Select node type based on whether full node is needed
if $USE_CELESTIA_FULL; then
    CELESTIA_MODE="full"
    CELESTIA_STORE="$USER_HOME/.celestia-full"
    CELESTIA_SERVICE_NAME="celestia-full"
else
    CELESTIA_MODE="light"
    CELESTIA_STORE="$USER_HOME/.celestia-light"
    CELESTIA_SERVICE_NAME="celestia-light"
fi

log "Celestia mode: ${CELESTIA_MODE}"

if [[ ! -d "$CELESTIA_STORE/keys" ]]; then
    log "Initializing Celestia ${CELESTIA_MODE} node..."
    INIT_OUTPUT=$(celestia "$CELESTIA_MODE" init 2>&1)
    echo "$INIT_OUTPUT"
    log "Celestia ${CELESTIA_MODE} node initialized."
else
    warn "Celestia ${CELESTIA_MODE} node already initialized."
fi

# Configure Celestia pruning window and sync start point (only on fresh init)
if [[ -f "$CELESTIA_STORE/config.toml" && ! -d "$CELESTIA_STORE/data" ]]; then
    log "Configuring Celestia pruning window..."
    sed -i "s|PruningWindow = .*|PruningWindow = \"${CELESTIA_PRUNING_WINDOW}\"|" "$CELESTIA_STORE/config.toml"
    if [[ -n "$CELESTIA_SYNC_FROM_HEIGHT" && -n "$CELESTIA_SYNC_FROM_HASH" ]]; then
        log "Setting Celestia sync start: height=$CELESTIA_SYNC_FROM_HEIGHT"
        sed -i "s|SyncFromHeight = .*|SyncFromHeight = ${CELESTIA_SYNC_FROM_HEIGHT}|" "$CELESTIA_STORE/config.toml"
        sed -i "s|SyncFromHash = .*|SyncFromHash = \"${CELESTIA_SYNC_FROM_HASH}\"|" "$CELESTIA_STORE/config.toml"
    fi
else
    warn "Celestia already has data, keeping existing sync config."
fi

# Extract auth token
log "Extracting Celestia auth token..."
CELESTIA_AUTH_TOKEN=$(celestia "$CELESTIA_MODE" auth admin --node.store "$CELESTIA_STORE" 2>/dev/null || true)

if [[ -z "$CELESTIA_AUTH_TOKEN" ]]; then
    # Try to start celestia briefly to generate the token
    warn "Could not get auth token yet. Will start Celestia to generate it..."
    celestia "$CELESTIA_MODE" start --core.ip "$CELESTIA_CORE_IP" --core.port "$CELESTIA_CORE_PORT" \
        --keyring.keyname my_celes_key &
    CELESTIA_PID=$!
    sleep 10
    CELESTIA_AUTH_TOKEN=$(celestia "$CELESTIA_MODE" auth admin --node.store "$CELESTIA_STORE" 2>/dev/null || true)
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

# Ensure local TCP connections use md5 auth (password) so DATABASE_URL works
PG_HBA=$(sudo -u postgres psql -t -P format=unaligned -c 'SHOW hba_file' 2>/dev/null | tr -d ' ')
if [[ -n "$PG_HBA" && -f "$PG_HBA" ]]; then
    # Replace 'peer' with 'md5' for local IPv4 connections to allow password auth
    sudo sed -i 's/^\(host\s\+all\s\+all\s\+127\.0\.0\.1\/32\s\+\)peer/\1md5/' "$PG_HBA"
    sudo sed -i 's/^\(host\s\+all\s\+all\s\+127\.0\.0\.1\/32\s\+\)ident/\1md5/' "$PG_HBA"
    sudo systemctl reload postgresql 2>/dev/null || true
fi

log "PostgreSQL configured (database: svm). Tables will be created by svm-rollup migrations."

# ---------------------------------------------------------------------------
# Step 9: Generate config.toml (skip if it already exists)
# ---------------------------------------------------------------------------
cd "$USER_HOME/svm-rollup"

if [[ ! -f "$USER_HOME/svm-rollup/config.toml" ]]; then
    log "Generating config.toml..."

    # Download template
    curl -fsSL "${REPO_URL}/config.toml.template" -o /tmp/config.toml.template 2>/dev/null || true

    # If curl failed (no internet or repo not public yet), use embedded template
    if [[ ! -f /tmp/config.toml.template || ! -s /tmp/config.toml.template ]] || ! grep -q '\[da\]' /tmp/config.toml.template 2>/dev/null; then
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
else
    warn "config.toml already exists, keeping existing configuration."
fi

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
mkdir -p "$USER_HOME/dashboard/templates" "$USER_HOME/dashboard/static"

curl -fsSL "${REPO_URL}/dashboard/app.py" -o "$USER_HOME/dashboard/app.py" 2>/dev/null || true
curl -fsSL "${REPO_URL}/dashboard/templates/index.html" -o "$USER_HOME/dashboard/templates/index.html" 2>/dev/null || true
curl -fsSL "${REPO_URL}/dashboard/static/logo.png" -o "$USER_HOME/dashboard/static/logo.png" 2>/dev/null || true

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

    curl -fsSL "$url" -o "$tmp" 2>/dev/null || true

    # If download failed, generate from embedded template
    if [[ ! -f "$tmp" || ! -s "$tmp" ]] || ! grep -q '\[Unit\]' "$tmp" 2>/dev/null; then
        warn "Could not download ${name}, using embedded version."
        case "$name" in
            solaxy-node.service)
cat > "$tmp" << EOF
[Unit]
Description=Solaxy SVM Rollup Node
After=network-online.target postgresql.service ${CELESTIA_SERVICE_NAME}.service
Wants=network-online.target
Requires=postgresql.service ${CELESTIA_SERVICE_NAME}.service

[Service]
User=${USER_NAME}
Type=simple
WorkingDirectory=${USER_HOME}/svm-rollup
Environment=SOV_PROVER_MODE=skip
Environment=RUST_LOG=info
Environment=DATABASE_URL=postgresql://postgres:secret@localhost:5432/svm
ExecStart=${USER_HOME}/svm-rollup/svm-rollup --da-layer celestia --rollup-config-path config.toml --genesis-config-dir genesis
Restart=on-failure
RestartSec=15
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
            ;;
            celestia-light.service|celestia-full.service)
                local cel_mode="${name%.service}"     # celestia-light or celestia-full
                cel_mode="${cel_mode#celestia-}"       # light or full
cat > "$tmp" << EOF
[Unit]
Description=Celestia ${cel_mode^} Node
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
Type=simple
ExecStart=/usr/local/bin/celestia ${cel_mode} start --core.ip ${CELESTIA_CORE_IP} --core.port ${CELESTIA_CORE_PORT} --keyring.keyname my_celes_key
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
    sed -i "s|%%USER%%|${USER_NAME}|g; s|%%HOME%%|${USER_HOME}|g; s|%%CELESTIA_SERVICE%%|${CELESTIA_SERVICE_NAME}|g; s|%%CELESTIA_MODE%%|${CELESTIA_MODE}|g" "$tmp"
    sudo cp "$tmp" "/etc/systemd/system/${name}"
    rm -f "$tmp"
}

install_service "solaxy-node.service"
install_service "${CELESTIA_SERVICE_NAME}.service"
install_service "solaxy-dashboard.service"

sudo systemctl daemon-reload
sudo systemctl enable "$CELESTIA_SERVICE_NAME" solaxy-node solaxy-dashboard

# Allow the dashboard to manage services without a password prompt
log "Configuring passwordless sudo for service management..."
sudo tee /etc/sudoers.d/solaxy-dashboard > /dev/null << EOF
${USER_NAME} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start solaxy-node.service, /usr/bin/systemctl stop solaxy-node.service, /usr/bin/systemctl restart solaxy-node.service
${USER_NAME} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ${CELESTIA_SERVICE_NAME}.service, /usr/bin/systemctl stop ${CELESTIA_SERVICE_NAME}.service, /usr/bin/systemctl restart ${CELESTIA_SERVICE_NAME}.service
${USER_NAME} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start solaxy-dashboard.service, /usr/bin/systemctl stop solaxy-dashboard.service, /usr/bin/systemctl restart solaxy-dashboard.service
EOF
sudo chmod 440 /etc/sudoers.d/solaxy-dashboard

# ---------------------------------------------------------------------------
# Step 14: Open firewall for dashboard
# ---------------------------------------------------------------------------
if command -v ufw &>/dev/null; then
    log "Opening firewall port 5555 (dashboard)..."
    sudo ufw allow 5555/tcp comment "Solaxy Dashboard" >/dev/null 2>&1
    log "Firewall port 5555 opened."
elif command -v firewall-cmd &>/dev/null; then
    log "Opening firewall port 5555 (dashboard)..."
    sudo firewall-cmd --permanent --add-port=5555/tcp >/dev/null 2>&1
    sudo firewall-cmd --reload >/dev/null 2>&1
    log "Firewall port 5555 opened."
else
    warn "No firewall tool (ufw/firewalld) found. Ensure port 5555 is accessible."
fi

# Start services (start is a no-op if already running; restart dashboard to pick up new files)
sudo systemctl start "$CELESTIA_SERVICE_NAME"
if ! systemctl is-active --quiet solaxy-node.service; then
    log "Waiting for Celestia to initialize..."
    sleep 15
fi
sudo systemctl start solaxy-node
sudo systemctl restart solaxy-dashboard

# ---------------------------------------------------------------------------
# Step 15: Print summary
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
echo -e "  ${YELLOW}Open the dashboard to set your password (first visit).${NC}"
echo ""
echo -e "  Celestia mode:  ${CYAN}${CELESTIA_MODE}${NC}"
echo ""
echo -e "  Service Status:"
echo -e "    ${CELESTIA_SERVICE_NAME}:    $(systemctl is-active ${CELESTIA_SERVICE_NAME}.service)"
echo -e "    solaxy-node:       $(systemctl is-active solaxy-node.service)"
echo -e "    solaxy-dashboard:  $(systemctl is-active solaxy-dashboard.service)"
echo -e "    postgresql:        $(systemctl is-active postgresql.service)"
echo ""
echo -e "  Config:       ${CYAN}${USER_HOME}/svm-rollup/config.toml${NC}"
echo -e "  Node Wallet:  ${CYAN}${USER_HOME}/svm-rollup/node-wallet.json${NC}"
echo -e "  Celestia:     ${CYAN}${CELESTIA_STORE}${NC}"
echo -e "  Logs:         journalctl -u solaxy-node -f"
echo ""
echo -e "${CYAN}============================================================${NC}"
