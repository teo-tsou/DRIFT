#!/bin/bash
# DRIFT Platform Installation Script
# Requires: Docker + docker compose v2

set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
print_header()  { echo -e "${BLUE}=====================================${NC}"; echo -e "${BLUE}$1${NC}"; echo -e "${BLUE}=====================================${NC}"; }
print_status()  { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

print_header "DRIFT Platform Installation"

# ── Bootstrap: ensure curl + ca-certificates ─────────────────────────────────
if ! command -v curl &>/dev/null; then
    print_status "Installing curl..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq curl ca-certificates
    elif command -v dnf &>/dev/null; then
        dnf install -y -q curl ca-certificates
    else
        print_error "Cannot install curl — unknown package manager. Install curl manually and re-run."
        exit 1
    fi
fi

# ── Detect OS ─────────────────────────────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_NAME="$NAME"
    OS_VER="$VERSION_ID"
else
    OS_NAME="Unknown"; OS_VER=""
fi
print_status "Detected OS: $OS_NAME $OS_VER"

# ── Install Docker if missing ─────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    print_status "Docker not found — installing via official script..."

    if [[ "$OS_NAME" == *"Ubuntu"* ]] || [[ "$OS_NAME" == *"Debian"* ]]; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq curl ca-certificates gnupg lsb-release

        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            > /etc/apt/sources.list.d/docker.list

        apt-get update -qq
        apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin

    elif [[ "$OS_NAME" == *"Red Hat"* ]] || [[ "$OS_NAME" == *"CentOS"* ]] || [[ "$OS_NAME" == *"Rocky"* ]]; then
        dnf install -y -q yum-utils
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y -q \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin

    else
        print_warning "Unknown OS — attempting Docker convenience script..."
        curl -fsSL https://get.docker.com | sh
    fi

    systemctl enable --now docker
    [[ $EUID -ne 0 ]] && usermod -aG docker "$USER" && \
        print_warning "Added $USER to docker group. Log out and back in, or run: newgrp docker"
    print_status "Docker installed."
else
    print_status "Docker already installed: $(docker --version | cut -d' ' -f3 | tr -d ',')"
fi

# ── Ensure Docker is running ──────────────────────────────────────────────────
if ! docker info &>/dev/null; then
    print_status "Starting Docker daemon..."
    systemctl start docker
    sleep 5
    if ! docker info &>/dev/null; then
        print_error "Docker daemon failed to start. Check: systemctl status docker"
        exit 1
    fi
fi

# ── Ensure docker compose v2 ──────────────────────────────────────────────────
if ! docker compose version &>/dev/null; then
    print_status "Installing docker-compose-plugin..."
    if [[ "$OS_NAME" == *"Ubuntu"* ]] || [[ "$OS_NAME" == *"Debian"* ]]; then
        apt-get install -y -qq docker-compose-plugin
    else
        dnf install -y -q docker-compose-plugin
    fi
fi
print_status "Docker Compose $(docker compose version --short)"

# ── SSH key ───────────────────────────────────────────────────────────────────
SSH_KEY="$HOME/.ssh/id_rsa"
if [[ ! -f "$SSH_KEY" ]]; then
    print_status "Generating SSH key pair at $SSH_KEY ..."
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    ssh-keygen -t rsa -b 4096 -f "$SSH_KEY" -N ""
    print_warning "SSH public key: ${SSH_KEY}.pub"
    print_warning "Copy it to edge nodes before adding them to clusters:"
    echo "    ssh-copy-id -i ${SSH_KEY}.pub ubuntu@<edge-node-ip>"
else
    print_status "SSH key found at $SSH_KEY"
fi

# ── Free ports needed by the stack ───────────────────────────────────────────
# (parse ss with grep, not a gawk-only 3-arg match(): Ubuntu 24.04 ships mawk)
for port in 3000 8000 5432; do
    pids=$(ss -tlnpH "sport = :${port}" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    for pid in $pids; do
        print_warning "Port $port in use by PID $pid — killing..."
        kill "$pid" 2>/dev/null || true
    done
done

# ── Write .env if not present ─────────────────────────────────────────────────
cd "$INSTALL_DIR"

ENV_FILE=".env"
if [[ ! -f "$ENV_FILE" ]]; then
    print_status "Creating $ENV_FILE with default settings..."
    cat > "$ENV_FILE" <<'EOF'
DATABASE_URL=postgresql://drift:drift@drift-postgres:5432/drift
EOF
    chmod 600 "$ENV_FILE"
    print_status "$ENV_FILE created."
else
    print_status "$ENV_FILE already exists — skipping."
fi

# ── Hub NFS checkpoint store + pod-migration credentials ──────────────────────
# Pod live-migration needs ONE shared checkpoint store both edge clusters mount,
# and ONE node-agent token so the backend drives either edge with one credential.
# The hub (this node) exports the store; the backend passes the server IP + token
# to the pod-migration playbook. Idempotent.
HUB_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | awk '{print $2}' | head -1)
HUB_IP=${HUB_IP:-$(hostname -I | awk '{print $1}')}
if ! grep -q '^DRIFT_CKPT_NFS_SERVER=' "$ENV_FILE"; then
    print_status "Setting up hub NFS checkpoint store export (server $HUB_IP:/srv/drift-ckpt)..."
    if command -v apt-get &>/dev/null; then apt-get install -y -qq nfs-kernel-server >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then dnf install -y -q nfs-utils >/dev/null 2>&1; fi
    mkdir -p /srv/drift-ckpt && chmod 777 /srv/drift-ckpt
    if ! grep -q '/srv/drift-ckpt' /etc/exports 2>/dev/null; then
        echo "/srv/drift-ckpt ${HUB_IP%.*}.0/24(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports
    fi
    exportfs -ra 2>/dev/null || true
    systemctl enable --now nfs-kernel-server 2>/dev/null || systemctl enable --now nfs-server 2>/dev/null || true
    {
        echo "DRIFT_CKPT_NFS_SERVER=${HUB_IP}"
        echo "DRIFT_NODE_AGENT_TOKEN=$(openssl rand -hex 24 2>/dev/null || head -c24 /dev/urandom | xxd -p)"
    } >> "$ENV_FILE"
    print_status "Checkpoint store exported; pod-migration credentials written to $ENV_FILE."
else
    print_status "Checkpoint store / pod-migration env already configured — skipping."
fi

# ── Start stack ───────────────────────────────────────────────────────────────
print_header "Starting DRIFT Stack"

print_status "Starting postgres + backend + frontend ..."
docker compose up -d --build

print_status "Waiting for backend to become healthy..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/health &>/dev/null || \
       curl -sf http://localhost:8000/docs &>/dev/null; then
        print_status "Backend is up."
        break
    fi
    [[ $i -eq 30 ]] && print_warning "Backend did not respond in time — check: docker compose logs backend"
    sleep 3
done

# ── Done ──────────────────────────────────────────────────────────────────────
print_header "Installation Complete"

echo ""
echo "  Frontend:   http://localhost:3000"
echo "  Backend:    http://localhost:8000"
echo "  API docs:   http://localhost:8000/docs"
echo ""
echo "Useful commands:"
echo "  docker compose ps                    — service status"
echo "  docker compose logs -f backend       — backend logs"
echo "  docker compose logs -f frontend      — frontend logs"
echo "  docker compose down                  — stop all services"
echo ""
print_status "DRIFT is ready."
