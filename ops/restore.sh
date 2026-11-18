#!/usr/bin/env bash
# Restore a backup made by ops/backup.sh into an EMPTY database (NFR12 restore drill, RTO 4 h).
# Usage: ops/restore.sh <backup_dir> <target_database_url> [object_dir]
set -euo pipefail
src="$1"; target="$2"; objdir="${3:-}"
( cd "$src" && sha256sum --check --quiet SHA256SUMS )
pg_restore --no-owner --exit-on-error --dbname="$target" "$src/masader.dump"
if [ -n "$objdir" ] && [ -f "$src/objects.tar.gz" ]; then mkdir -p "$objdir"; tar -C "$objdir" -xzf "$src/objects.tar.gz"; fi
echo "restored $src into $target"
