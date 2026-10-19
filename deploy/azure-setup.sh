#!/usr/bin/env bash
# One-shot server setup for SiteScope on a fresh Ubuntu 24.04 VM.
# Used as Azure "Custom data" at VM creation:
#   #!/bin/bash
#   curl -fsSL https://raw.githubusercontent.com/vikramyadav1404/sitescope/main/deploy/azure-setup.sh | bash
# Log: /var/log/sitescope-setup.log
set -euo pipefail
exec > >(tee -a /var/log/sitescope-setup.log) 2>&1
echo "== SiteScope setup started $(date -u)"

export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/sitescope
DOMAIN=vikramyadav.me

apt-get update -y
apt-get upgrade -y
apt-get install -y git curl ufw nginx certbot python3-certbot-nginx dnsutils openssl

# firewall: SSH, HTTP, HTTPS only
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 1 GB swap for small VMs
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Docker
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

# App
if [ ! -d "$APP_DIR/.git" ]; then
  git clone https://github.com/vikramyadav1404/sitescope.git "$APP_DIR"
fi
cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
  chmod 600 .env
fi
docker compose up -d --build

# Nginx reverse proxy
cp deploy/nginx-sitescope.conf /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# wait for the app
for i in $(seq 1 30); do
  if curl -fs http://127.0.0.1:8000/health >/dev/null; then break; fi
  sleep 5
done
curl -s http://127.0.0.1:8000/health && echo
echo "== SiteScope setup finished $(date -u)"
echo "Next: point DNS to this VM, then run: sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN"
