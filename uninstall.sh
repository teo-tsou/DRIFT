#!/bin/bash
# DRIFT Platform — Uninstall Script
# Stops and removes all containers, volumes, images, and generated config.
# USE WITH CAUTION — all cluster state and data will be lost.

set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
print_header()  { echo -e "${BLUE}=====================================${NC}"; echo -e "${BLUE}$1${NC}"; echo -e "${BLUE}=====================================${NC}"; }
print_status()  { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

print_header "DRIFT Platform Uninstall"

echo ""
print_warning "This will PERMANENTLY remove:"
echo "  • All DRIFT Docker containers (backend, frontend, postgres)"
echo "  • All DRIFT Docker volumes  (postgres_data — ALL DATA LOST)"
echo "  • The DRIFT backend image"
echo "  • Generated config          (.env)"
echo "  • Generated kubeconfigs     (backend/kubeconfigs/)"
echo ""
print_warning "This action CANNOT be undone!"
echo ""

if [[ "${1}" != "--force" ]]; then
    read -p "Type 'YES' to confirm: " confirmation
    if [[ "$confirmation" != "YES" ]]; then
        print_status "Uninstall cancelled."
        exit 0
    fi
fi

if ! command -v docker &>/dev/null || ! docker info &>/dev/null; then
    print_error "Docker is not available. Cannot proceed."
    exit 1
fi

cd "$INSTALL_DIR"

# ── Stop stack ────────────────────────────────────────────────────────────────
print_header "Stopping Services"

if [[ -f "docker-compose.yml" ]]; then
    print_status "Stopping all DRIFT services and removing volumes..."
    docker compose down -v --remove-orphans 2>/dev/null || true
fi

# ── Remove any standalone containers ─────────────────────────────────────────
for container in drift-postgres drift-backend drift-frontend; do
    if docker ps -a --format "{{.Names}}" | grep -q "^${container}$"; then
        print_status "Removing container: $container"
        docker stop "$container" 2>/dev/null || true
        docker rm   "$container" 2>/dev/null || true
    fi
done

# ── Remove volumes ────────────────────────────────────────────────────────────
for volume in drift_postgres_data; do
    if docker volume ls --format "{{.Name}}" | grep -q "^${volume}$"; then
        print_status "Removing volume: $volume"
        docker volume rm "$volume" 2>/dev/null || true
    fi
done

# ── Remove built images ───────────────────────────────────────────────────────
print_header "Removing Images"

for image in drift-backend; do
    if docker image inspect "$image" &>/dev/null; then
        print_status "Removing image: $image"
        docker rmi "$image" -f 2>/dev/null || true
    fi
done

# ── Remove generated files ────────────────────────────────────────────────────
print_header "Removing Generated Files"

ENV_FILE=".env"
if [[ -f "$ENV_FILE" ]]; then
    print_status "Removing $ENV_FILE"
    rm -f "$ENV_FILE"
fi

KUBECONFIGS_DIR="backend/kubeconfigs"
if [[ -d "$KUBECONFIGS_DIR" ]] && [[ -n "$(ls -A "$KUBECONFIGS_DIR" 2>/dev/null)" ]]; then
    print_status "Removing backend/kubeconfigs/ ($(ls "$KUBECONFIGS_DIR" | wc -l) files)"
    rm -rf "$KUBECONFIGS_DIR"
fi

# ── Free ports ────────────────────────────────────────────────────────────────
print_header "Freeing Ports"

for port in 3000 8000 5432; do
    pid=$(ss -tlnp 2>/dev/null | awk -v p=":${port} " '$0 ~ p {match($0,/pid=([0-9]+)/,a); if(a[1]) print a[1]}')
    if [[ -n "$pid" ]]; then
        print_status "Killing PID $pid holding port $port..."
        kill "$pid" 2>/dev/null || true
    fi
done

# ── Prune orphaned Docker resources ──────────────────────────────────────────
print_header "Pruning Orphaned Resources"

docker network prune -f 2>/dev/null || true
docker image prune -f 2>/dev/null || true

# ── Verify ────────────────────────────────────────────────────────────────────
print_header "Verification"

issues=0
for container in drift-postgres drift-backend drift-frontend; do
    if docker ps -a --format "{{.Names}}" | grep -q "^${container}$"; then
        print_warning "Container still exists: $container"
        ((issues++)) || true
    fi
done

for volume in drift_postgres_data; do
    if docker volume ls --format "{{.Name}}" | grep -q "^${volume}$"; then
        print_warning "Volume still exists: $volume"
        ((issues++)) || true
    fi
done

if [[ -f "$ENV_FILE" ]]; then
    print_warning "$ENV_FILE still exists"
    ((issues++)) || true
fi

if [[ $issues -eq 0 ]]; then
    print_status "All DRIFT components removed successfully."
else
    print_warning "$issues item(s) could not be removed — check manually:"
    echo "  docker ps -a && docker volume ls"
fi

echo ""
print_header "Uninstall Complete"
echo ""
print_status "Docker itself is still installed and running."
print_status "To reinstall DRIFT: sudo bash install.sh"
echo ""
