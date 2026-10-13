#!/usr/bin/env bash
# MSP API Tools — HTTPS edge for external (Claude) access.
#
#   sudo DOMAIN=tools.example.com EMAIL=admin@example.com bash deploy/edge/setup-edge.sh http
#   sudo DOMAIN=... EMAIL=... bash deploy/edge/setup-edge.sh godaddy   # cert via GoDaddy DNS API (no port 80)
#
# Required: DOMAIN (public hostname of this server), EMAIL (Let's Encrypt expiry notices).
# Optional: OFFICE_IPS — your office's public IPs, space separated, so office browsers that arrive
#           through the public address (NAT loopback) get the full app instead of only MCP/OAuth.
#
# Installs nginx + acme.sh, issues a Let's Encrypt cert for $DOMAIN, auto-renews it (acme.sh cron,
# reloads nginx), and proxies ONLY the MCP + OAuth endpoints to the app on 127.0.0.1:8000 with
# X-MSP-Edge: 1 so the app requires a connection key / OAuth token. Safe to re-run.
set -euo pipefail

MODE="${1:-}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"                                    # Let's Encrypt expiry notices
UPSTREAM="${UPSTREAM:-http://127.0.0.1:8000}"
OFFICE_IPS="${OFFICE_IPS:-}"
# Second layer behind your firewall: Anthropic egress (Claude connectors) + private ranges + office public IPs
ALLOW_CIDRS="${ALLOW_CIDRS:-127.0.0.1 160.79.104.0/21 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12 $OFFICE_IPS}"
# Sources that get the FULL app over https (dashboard, /changes, sign-in): office, LAN, VPN.
INTERNAL_CIDRS="${INTERNAL_CIDRS:-127.0.0.1 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12 $OFFICE_IPS}"
ACME_HOME="/root/.acme.sh"
SSL_DIR="/etc/nginx/ssl"
WEBROOT="/var/www/acme"
SITE="/etc/nginx/sites-available/msp-edge"

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ "$MODE" = "godaddy" ] || [ "$MODE" = "http" ] || die "usage: sudo DOMAIN=... EMAIL=... bash $0 godaddy|http"
[ -n "$DOMAIN" ] || die "set DOMAIN to this server's public hostname, e.g. DOMAIN=tools.example.com"
[ -n "$EMAIL" ] || die "set EMAIL for Let's Encrypt expiry notices, e.g. EMAIL=admin@example.com"

echo "==> Checking ports 80/443"
if ss -tlnp | grep -E ':(80|443)\s' | grep -vq nginx; then
  ss -tlnp | grep -E ':(80|443)\s'
  die "ports 80/443 are used by something other than nginx"
fi

# Only listen on IPv6 if the kernel has it enabled (nginx won't start otherwise).
if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 1)" = "0" ]; then
  L6_80="listen [::]:80;"; L6_443="listen [::]:443 ssl;"
else
  L6_80=""; L6_443=""
fi

echo "==> Installing packages"
apt-get update -q
mkdir -p "$SSL_DIR" "$WEBROOT"
# Remove the stock site BEFORE nginx's first start (it also listens on [::]:80).
rm -f /etc/nginx/sites-enabled/default
DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx curl socat cron openssl \
  || { rm -f /etc/nginx/sites-enabled/default; dpkg --configure -a; }
rm -f /etc/nginx/sites-enabled/default
systemctl enable --now cron
systemctl enable nginx
systemctl restart nginx || { nginx -t; journalctl -u nginx --no-pager -n 20; die "nginx failed to start"; }

echo "==> Installing acme.sh"
if [ ! -x "$ACME_HOME/acme.sh" ]; then
  curl -fsSL https://get.acme.sh | sh -s email="$EMAIL" >/dev/null
fi
"$ACME_HOME/acme.sh" --set-default-ca --server letsencrypt >/dev/null
"$ACME_HOME/acme.sh" --upgrade --auto-upgrade >/dev/null || true

allow_block() {
  for c in $ALLOW_CIDRS; do echo "    allow $c;"; done
  echo "    deny all;"
}

write_http_only() {
  cat > "$SITE" <<EOF
server {
    listen 80;
    $L6_80
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 444; }
}
EOF
  ln -sf "$SITE" /etc/nginx/sites-enabled/msp-edge
  nginx -t && systemctl reload nginx
}

# $1 = upstream path. Always the *normalized* path, never the client's raw URI,
# so tricks like /oauth/token/../../api/x can't reach other app routes.
proxy_common() {
  cat <<EOF
        limit_except GET POST { deny all; }
        proxy_pass $UPSTREAM$1;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-MSP-Edge 1;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        proxy_buffering off;
EOF
}

internal_geo() {
  echo "geo \$msp_internal {"
  echo "    default 0;"
  for c in $INTERNAL_CIDRS; do echo "    $c 1;"; done
  echo "}"
}

# Full app for office/LAN/VPN only — all methods, normalized path, larger uploads.
proxy_internal() {
  cat <<EOF
        proxy_pass $UPSTREAM\$uri\$is_args\$args;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-MSP-Edge 1;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        client_max_body_size 25m;
EOF
}

write_full() {
  cat > "$SITE" <<EOF
# Managed by deploy/edge/setup-edge.sh
limit_req_zone \$binary_remote_addr zone=msp_mcp:10m rate=20r/s;
limit_req_zone \$binary_remote_addr zone=msp_oauth:10m rate=30r/m;
$(internal_geo)

server {
    listen 80;
    $L6_80
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 444; }
}

server {
    listen 443 ssl;
    $L6_443
    server_name $DOMAIN;

    ssl_certificate     $SSL_DIR/$DOMAIN.crt;
    ssl_certificate_key $SSL_DIR/$DOMAIN.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:msp:10m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    server_tokens off;
    limit_req_status 429;
    client_max_body_size 1m;
    access_log /var/log/nginx/msp-edge.access.log;
    error_log  /var/log/nginx/msp-edge.error.log;

$(allow_block)

    # MCP servers (app requires a connection key or OAuth token for edge traffic): the MSP pack's /msp/mcp,
    # the hub /mcp, one per connection /mcp/<slug>, and groups /mcp/<slug>/<group>
    location = /msp/mcp {
        limit_req zone=msp_mcp burst=40 nodelay;
$(proxy_common /msp/mcp)
    }
    location ~ ^/mcp(/[a-z][a-z0-9_]{1,40}(/[a-z0-9][a-z0-9_-]{0,39})?)?$ {
        limit_req zone=msp_mcp burst=40 nodelay;
$(proxy_common '$uri')
    }

    # OAuth discovery, client registration and token exchange (called by Anthropic's servers)
    location ~ ^/(\.well-known/oauth-(protected-resource|authorization-server)(/msp/mcp|/mcp(/[a-z][a-z0-9_]{1,40}(/[a-z0-9][a-z0-9_-]{0,39})?)?)?|oauth/(register|token))$ {
        limit_req zone=msp_oauth burst=20 nodelay;
$(proxy_common '$uri$is_args$args')
    }

    # Everything else (dashboard, /changes, /auth sign-in, /oauth/authorize): office/LAN/VPN only
    location / {
        if (\$msp_internal = 0) { return 404; }
$(proxy_internal)
    }
}
EOF
  ln -sf "$SITE" /etc/nginx/sites-enabled/msp-edge
  nginx -t && systemctl reload nginx
}

if [ ! -s "$SSL_DIR/$DOMAIN.crt" ]; then
  write_http_only
  echo "==> Issuing certificate for $DOMAIN ($MODE)"
  if [ "$MODE" = "godaddy" ]; then
    if [ -z "${GD_Key:-}" ] && ! grep -q "SAVED_GD_Key" "$ACME_HOME/account.conf" 2>/dev/null; then
      read -r -s -p "GoDaddy API key: " GD_Key; echo
      read -r -s -p "GoDaddy API secret: " GD_Secret; echo
      export GD_Key GD_Secret
    fi
    if [ -n "${GD_Key:-}" ]; then
      ROOT_DOMAIN="${DOMAIN#*.}"
      code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: sso-key $GD_Key:$GD_Secret" \
        "https://api.godaddy.com/v1/domains/$ROOT_DOMAIN")
      [ "$code" = "200" ] || die "GoDaddy API returned HTTP $code for $ROOT_DOMAIN. GoDaddy limits API access on smaller accounts — re-run with: sudo bash $0 http"
    fi
    "$ACME_HOME/acme.sh" --issue --dns dns_gd -d "$DOMAIN" --keylength ec-256 || [ $? -eq 2 ]
  else
    "$ACME_HOME/acme.sh" --issue -w "$WEBROOT" -d "$DOMAIN" --keylength ec-256 || [ $? -eq 2 ]
  fi
fi

echo "==> Installing certificate + renewal hook"
"$ACME_HOME/acme.sh" --install-cert -d "$DOMAIN" --ecc \
  --key-file "$SSL_DIR/$DOMAIN.key" \
  --fullchain-file "$SSL_DIR/$DOMAIN.crt" \
  --reloadcmd "systemctl reload nginx" >/dev/null
chmod 600 "$SSL_DIR/$DOMAIN.key"

write_full

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "==> Opening ufw 443$( [ "$MODE" = http ] && echo ' and 80')"
  ufw allow 443/tcp >/dev/null
  [ "$MODE" = "http" ] && ufw allow 80/tcp >/dev/null
fi

echo
echo "==> Done"
openssl x509 -in "$SSL_DIR/$DOMAIN.crt" -noout -subject -enddate
crontab -l 2>/dev/null | grep acme.sh || echo "WARNING: acme.sh cron entry not found"
echo
echo "Local test (expect 401 = key required):"
curl -s -o /dev/null -w '  /msp/mcp -> %{http_code}\n' --resolve "$DOMAIN:443:127.0.0.1" \
  -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' "https://$DOMAIN/msp/mcp"
curl -s -o /dev/null -w '  / from this server (internal: dashboard, expect 200) -> %{http_code}\n' --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/"
echo "Renewal is automatic (acme.sh daily cron, renews ~30 days before expiry, reloads nginx)."
echo "Check renewal status: sudo $ACME_HOME/acme.sh --list"
echo "Force a renewal (counts toward Let's Encrypt rate limits — use sparingly): sudo $ACME_HOME/acme.sh --renew -d $DOMAIN --ecc --force"
