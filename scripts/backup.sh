#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$BACKUP_DIR/aigopushtg-$STAMP.dump.tmp"
OUT="$BACKUP_DIR/aigopushtg-$STAMP.dump"

docker compose exec -T postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$TMP"
test -s "$TMP"
mv "$TMP" "$OUT"
chmod 600 "$OUT"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'aigopushtg-*.dump' -mtime +"$RETENTION_DAYS" -delete
printf 'Backup created: %s\n' "$OUT"
