#!/usr/bin/env bash
# Allow HTTP/HTTPS (80, 443) only from Cloudflare's IP ranges, so the origin VM
# can't be reached directly by its IP. SSH stays open.
#
# Run AFTER Certbot has issued the certificate and the Cloudflare records are
# orange (proxied). The first certificate needs Let's Encrypt to reach the VM
# directly; renewals go through Cloudflare and keep working.
#
# Usage on the VM:   sudo bash deploy/cloudflare-ufw.sh
# Safe to re-run: it fetches the current ranges and replaces the old rules.
# Optional weekly refresh (root crontab):
#   17 4 * * 1  bash /opt/sitescope/deploy/cloudflare-ufw.sh >> /var/log/cloudflare-ufw.log 2>&1
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

TAG="cloudflare"
v4="$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4)"
v6="$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6)"
ranges="$(printf '%s\n%s\n' "$v4" "$v6" | grep -E '^[0-9a-fA-F:.]+/[0-9]+$' || true)"

# Refuse to continue on an empty or odd download, or the site would go dark.
count="$(printf '%s\n' "$ranges" | grep -c . || true)"
if [ "$count" -lt 10 ]; then
  echo "Got only $count ranges from cloudflare.com; not changing the firewall." >&2
  exit 1
fi

# SSH first, so this script can never lock you out.
ufw allow OpenSSH >/dev/null

# Remove old Cloudflare rules and the open-to-all 80/443 rules (highest number first).
delete_matching() {
  ufw status numbered \
    | { grep -E "$1" || true; } \
    | sed -E 's/^\[ *([0-9]+)\].*/\1/' \
    | sort -rn \
    | while read -r n; do ufw --force delete "$n" >/dev/null; done
}
delete_matching "# ${TAG}\$"
delete_matching '^\[ *[0-9]+\] (80|443)(/tcp)?( \(v6\))? +ALLOW IN +Anywhere'
delete_matching "^\[ *[0-9]+\] (Nginx Full|Nginx HTTP|Nginx HTTPS)( \(v6\))? +ALLOW IN +Anywhere"

while read -r cidr; do
  ufw allow proto tcp from "$cidr" to any port 80,443 comment "$TAG" >/dev/null
done <<< "$ranges"

ufw --force enable >/dev/null
echo "Allowed 80/443 from $count Cloudflare ranges. SSH is still open."
ufw status | grep -E "OpenSSH|22|${TAG}" | head -n 5
echo "..."
echo "Check: curl -sI https://vikramyadav.me | head -n 1   (should still be 200)"
