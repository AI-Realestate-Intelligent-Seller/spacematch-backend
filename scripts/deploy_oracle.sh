#!/usr/bin/env bash
# Deploy (or redeploy) SpaceMatch on an Oracle Cloud Ubuntu VM: Docker container + Caddy (automatic HTTPS).
#
#   First time:  cp .env.example .env && nano .env      (BASE_URL=https://<domain>, secrets, DATABASE_URL...)
#   Every time:  git pull && bash scripts/deploy_oracle.sh <domain>
#
# <domain> is your own domain pointed at the VM, or <ip-with-dashes>.sslip.io (e.g. 140-238-1-2.sslip.io).
# Safe to run repeatedly: it only installs what is missing, then rebuilds and restarts the app.
set -euo pipefail

DOMAIN="${1:?usage: bash scripts/deploy_oracle.sh <domain, e.g. 140-238-1-2.sslip.io>}"
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env here. Run: cp .env.example .env  and fill it in first."; exit 1; }
grep -q "^BASE_URL=https://$DOMAIN/\?$" .env || echo "WARNING: .env should contain BASE_URL=https://$DOMAIN (logins need it)."

echo "== Firewall: open ports 80 and 443 (Oracle's Ubuntu image blocks them in iptables)"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    reject=$(sudo iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')
    sudo iptables -I INPUT "${reject:-1}" -p tcp --dport "$port" -j ACCEPT
  fi
done
command -v netfilter-persistent >/dev/null && sudo netfilter-persistent save >/dev/null

echo "== Docker and Caddy"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh
command -v caddy >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq caddy; }

echo "== Build the image (first time ~5-10 min: installs Chromium)"
sudo docker build -t space-match .

echo "== (Re)start the app"
mkdir -p data && sudo chown 1000:1000 data   # SQLite file survives rebuilds (only used without DATABASE_URL)
sudo docker rm -f space-match >/dev/null 2>&1 || true
sudo docker run -d --name space-match --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8000:8000 --shm-size=1g \
  -v "$PWD/data:/app/data" space-match

echo "== HTTPS via Caddy for $DOMAIN"
printf '%s {\n    reverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy

echo "== Waiting for the app"
for _ in $(seq 1 30); do
  if curl -fs -o /dev/null http://127.0.0.1:8000/; then
    echo "App is up. Open https://$DOMAIN  (the certificate can take a minute the first time)."
    exit 0
  fi
  sleep 2
done
echo "App did not answer. Recent logs:"; sudo docker logs --tail 40 space-match; exit 1
