#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# MSP API Tools — pull latest code & restart on Linux server
# Run on the SERVER after pushing from a developer machine.
# Usage:   bash deploy/pull-and-restart.sh
#          ── OR via SSH from Windows ──
#          ssh deploy@server "cd /opt/msp-api-tools && bash deploy/pull-and-restart.sh"
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/msp-api-tools}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

cd "${INSTALL_DIR}"

echo "▶ Pulling latest changes"
git pull --ff-only origin master

echo "▶ Rebuilding & restarting app container"
docker compose "${COMPOSE_FILES[@]}" up -d --build app

echo "▶ Pruning dangling images"
docker image prune -f >/dev/null

echo
echo "▶ Service status:"
docker compose "${COMPOSE_FILES[@]}" ps

echo
echo "▶ Last 20 lines of app log:"
sleep 3
docker compose "${COMPOSE_FILES[@]}" logs --tail=20 app

echo
echo "✅ Deploy complete. Health check:"
if curl -fsS "http://127.0.0.1:8000/api/connectors" >/dev/null 2>&1; then
  echo "   API responding on port 8000"
else
  echo "   ⚠ API not responding — check logs above"
fi
