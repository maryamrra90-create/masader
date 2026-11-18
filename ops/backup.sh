#!/usr/bin/env bash
# Daily backup (NFR12, RPO 24 h): a custom-format pg_dump of the masader database plus the object store.
# Usage: ops/backup.sh [backup_dir]    Needs DATABASE_URL and OBJECT_DIR (as in .env). Run from cron or a
# systemd timer on the host; copy the backup directory off the host (e.g. to S3) afterwards.
set -euo pipefail
dir="${1:-./backups}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$dir"
pg_dump --format=custom --no-owner --dbname="$DATABASE_URL" --file="$dir/masader.dump"
if [ -d "${OBJECT_DIR:-}" ]; then tar -C "$OBJECT_DIR" -czf "$dir/objects.tar.gz" .; fi
( cd "$dir" && sha256sum ./* > SHA256SUMS )
echo "$dir"
