#!/usr/bin/env bash
# Nightly encrypted backup to Cloudflare R2. Runs on the server.
#   ./backup.sh             dump, encrypt, upload, prune
#   ./backup.sh --stdout    dump and encrypt to stdout only (no R2) — for a
#                           restore check before R2 is set up
#
# Installed in root's crontab on the server:
#   0 3 * * * cd /opt/secondmind/deploy && COMPOSE_FILE=docker-compose.shared.yml ./backup.sh >> /var/log/secondmind-backup.log 2>&1
#
# With audio discarded, this dump is the ONLY copy of your transcripts that
# exists anywhere. Two consequences, both non-negotiable:
#   - it is encrypted before it leaves the box, with a key whose private
#     half lives somewhere else (a backup encrypted with a key sitting next
#     to it protects nothing) — this box can write backups, never read them;
#   - verify_backup.sh restores one for real, regularly. An untested backup
#     is not a backup.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
set -a; . "$HERE/../backend/.env"; set +a

: "${DB_USER:?}" "${DB_NAME:?}"
: "${BACKUP_AGE_PUBLIC_KEY:?set BACKUP_AGE_PUBLIC_KEY in backend/.env}"

dump() {
  docker compose -f "$HERE/$COMPOSE_FILE" --env-file "$HERE/../backend/.env" exec -T postgres \
    pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner \
    | age -r "$BACKUP_AGE_PUBLIC_KEY"
}

if [ "${1:-}" = "--stdout" ]; then
  dump
  exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="secondmind-${STAMP}.dump.age"
ARTIFACT="/tmp/${NAME}"
trap 'rm -f "$ARTIFACT"' EXIT

echo "==> $(date -u) dumping and encrypting"
dump > "$ARTIFACT"

SIZE=$(wc -c < "$ARTIFACT")
# A dump that shrank to nothing means pg_dump failed inside the pipe and
# set -o pipefail didn't catch it (age still exits 0 on empty input).
# Uploading that over a good backup is how people discover they have none.
if [ "$SIZE" -lt 10000 ]; then
  echo "Refusing to upload: dump is only ${SIZE} bytes — pg_dump probably failed." >&2
  exit 1
fi

echo "==> Uploading ${SIZE} bytes as ${NAME}"
"$HERE/r2.sh" put "$ARTIFACT" "$NAME"

echo "==> Pruning backups older than 90 days"
"$HERE/r2.sh" prune 90

echo "==> Done: ${NAME}"
