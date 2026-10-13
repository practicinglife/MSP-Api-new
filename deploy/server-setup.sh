#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# MSP API Tools — first-time Linux server setup
# Tested on Ubuntu 22.04 / 24.04 LTS and Debian 12.
# Run ONCE on the server as root (or with sudo).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<your-org>/<your-repo>/master/deploy/server-setup.sh | sudo bash
#   ── OR ──
#   sudo bash deploy/server-setup.sh
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="${REPO_URL:?set REPO_URL to your fork, e.g. REPO_URL=https://github.com/<your-org>/<your-repo>.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/msp-api-tools}"
APP_PORT="${APP_PORT:-8000}"
RUN_AS_USER="${RUN_AS_USER:-deploy}"

log() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m⚠ %s\033[0m\n" "$*"; }

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must run as root. Re-run with: sudo bash $0"
  exit 1
fi

# ── 1. System packages ──────────────────────────────────────────
log "Updating apt and installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl gnupg git ufw lsb-release jq \
  unattended-upgrades

# ── 2. Docker Engine + compose plugin ───────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  CODENAME=$(. /etc/os-release; echo "$VERSION_CODENAME")
  OS_ID=$(. /etc/os-release; echo "$ID")
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${OS_ID} ${CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  log "Docker already installed: $(docker --version)"
fi

# ── 3. Add the run-as user to the docker group ──────────────────
if id "$RUN_AS_USER" >/dev/null 2>&1; then
  log "Adding ${RUN_AS_USER} to docker group"
  usermod -aG docker "$RUN_AS_USER"
else
  warn "User ${RUN_AS_USER} does not exist — skipping docker group membership."
fi

# ── 4. Clone the repository ─────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Repo already cloned at ${INSTALL_DIR} — pulling latest"
  git -C "${INSTALL_DIR}" pull --ff-only
else
  log "Cloning ${REPO_URL} → ${INSTALL_DIR}"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

# Ownership for non-root operations
if id "$RUN_AS_USER" >/dev/null 2>&1; then
  chown -R "${RUN_AS_USER}:${RUN_AS_USER}" "${INSTALL_DIR}"
fi

# ── 5. .env file scaffold ───────────────────────────────────────
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  log "Creating .env from template (you MUST fill in real secrets)"
  cp "${INSTALL_DIR}/deploy/.env.production.template" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
  if id "$RUN_AS_USER" >/dev/null 2>&1; then
    chown "${RUN_AS_USER}:${RUN_AS_USER}" "${INSTALL_DIR}/.env"
  fi
  warn "Edit ${INSTALL_DIR}/.env with your real API keys before starting the app."
else
  log ".env already exists at ${INSTALL_DIR}/.env — leaving untouched"
fi

# ── 6. Firewall (UFW) ───────────────────────────────────────────
log "Configuring UFW firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow "${APP_PORT}/tcp" comment "MSP API Tools"
# Postgres should normally NOT be exposed publicly:
# ufw allow 5432/tcp comment "Postgres (only if you trust the network!)"
ufw --force enable

# ── 7. Auto-update ──────────────────────────────────────────────
log "Enabling unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

# ── 8. Done ─────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────────────────────────"
echo "✅ Server setup complete."
echo
echo "Next steps:"
echo "  1. Edit ${INSTALL_DIR}/.env with real API keys"
echo "  2. cd ${INSTALL_DIR} && bash deploy/pull-and-restart.sh"
echo "  3. Visit https://<server-ip>:${APP_PORT}/setup"
echo "────────────────────────────────────────────────────────────"
