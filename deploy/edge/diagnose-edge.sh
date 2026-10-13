#!/usr/bin/env bash
# Collects everything needed to debug the HTTPS edge. Read-only.
# Run: sudo DOMAIN=tools.example.com bash deploy/edge/diagnose-edge.sh
DOMAIN="${DOMAIN:-$(sed -n 's/^\s*server_name\s\+\([^ ;]\+\).*/\1/p' /etc/nginx/sites-available/msp-edge 2>/dev/null | head -1)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
s() { echo; echo "===== $* ====="; }
s "os"; . /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"; uname -r
s "nginx installed?"; command -v nginx && nginx -v 2>&1 || echo "nginx NOT installed"
s "nginx service"; systemctl status nginx --no-pager -l 2>&1 | head -25
s "nginx -t"; nginx -t 2>&1
s "listening on 80/443"; ss -tlnp | grep -E ':(80|443)\s' || echo "nothing on 80/443"
s "ipv6 enabled?"; cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null
s "sites-enabled"; ls -la /etc/nginx/sites-enabled/ 2>&1
s "site config"; cat /etc/nginx/sites-available/msp-edge 2>&1 | head -60
s "certs"; ls -la /etc/nginx/ssl/ 2>&1
s "acme.sh"; ls -la /root/.acme.sh/acme.sh 2>&1; /root/.acme.sh/acme.sh --list 2>&1 | head
s "acme.sh log (tail)"; tail -40 /root/.acme.sh/acme.sh.log 2>&1
s "acme cron"; crontab -l 2>&1 | grep -i acme || echo "no acme cron"
s "nginx error log"; tail -25 /var/log/nginx/error.log 2>&1
s "journal nginx"; journalctl -u nginx --no-pager -n 25 2>&1
s "ufw"; ufw status 2>&1
s "app on 8000"; curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
s "dns ${DOMAIN:-<DOMAIN not set>}"; [ -n "$DOMAIN" ] && { getent hosts "$DOMAIN" || echo "does not resolve from server"; }
s "script line endings"; file "$SCRIPT_DIR/setup-edge.sh" 2>&1
