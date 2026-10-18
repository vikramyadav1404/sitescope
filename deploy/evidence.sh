#!/usr/bin/env bash
# Prints the server facts the CSET489 report asks for (Sections 7-8, Appendix A).
# Usage on the VM:  bash deploy/evidence.sh vikramyadav.me | tee evidence.txt
# It prints no passwords, keys or tokens.
DOMAIN="${1:-vikramyadav.me}"
run() { echo; echo "\$ $*"; eval "$@" 2>&1; }

echo "SiteScope deployment evidence - $(date -u '+%d-%m-%Y %H:%M UTC')"
run "lsb_release -ds"
run "uname -r"
run "nginx -v"
run "docker --version && docker compose version"
run "sudo docker compose ps"
run "sudo docker compose exec -T db psql -U sitescope -d sitescope -tAc 'select version();'"
run "sudo ufw status"
run "sudo nginx -t"
run "dig +short NS $DOMAIN"
run "dig +short $DOMAIN"
run "curl -sI https://$DOMAIN | head -n 12"
run "curl -s https://$DOMAIN/health"
run "sudo certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry'"
