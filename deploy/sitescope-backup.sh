#!/bin/bash
# Daily SiteScope Postgres backup: custom-format pg_dump, checked readable,
# newest 14 kept on the VM, and a copy in Azure Blob (sitescopedbbackup /
# sitescope-db/daily, deleted there after 35 days). Upload uses the VM's
# managed identity.
set -euo pipefail
DIR=/var/backups/sitescope
KEEP=14
DEST=https://sitescopedbbackup.blob.core.windows.net/sitescope-db/daily
F="$DIR/sitescope-$(date -u +%Y%m%d-%H%M%S).dump"
cd /opt/sitescope
docker compose exec -T db pg_dump -U sitescope -d sitescope -Fc > "$F.part"
docker compose exec -T db pg_restore --list < "$F.part" > /dev/null
mv "$F.part" "$F"; chmod 600 "$F"
ls -1t "$DIR"/sitescope-*.dump | tail -n +$((KEEP+1)) | xargs -r rm -f
export AZCOPY_AUTO_LOGIN_TYPE=MSI AZCOPY_LOG_LOCATION=/var/log/azcopy AZCOPY_JOB_PLAN_LOCATION=/var/lib/azcopy/plans
mkdir -p "$AZCOPY_LOG_LOCATION" "$AZCOPY_JOB_PLAN_LOCATION"
azcopy copy "$F" "$DEST/$(basename "$F")" --output-level quiet --log-level ERROR
find "$AZCOPY_LOG_LOCATION" "$AZCOPY_JOB_PLAN_LOCATION" -type f -mtime +14 -delete
date -u +%FT%TZ > "$DIR/LAST_OK"
echo "backup ok: $(basename "$F") $(stat -c %s "$F") bytes, $(ls "$DIR"/sitescope-*.dump | wc -l) kept locally, copied off-VM"
