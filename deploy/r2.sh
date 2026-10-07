#!/usr/bin/env bash
# The one place that knows how to reach the R2 backup bucket. Runs on the
# server, which holds the R2 credentials (never the decryption key).
#   ./r2.sh ls                 backups, oldest first
#   ./r2.sh latest             name of the newest backup
#   ./r2.sh cat <name>         stream one (still encrypted) to stdout
#   ./r2.sh put <file> <name>  upload
#   ./r2.sh prune <days>       delete backups older than <days>
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/../backend/.env"; set +a
: "${R2_BUCKET:?}" "${R2_ACCOUNT_ID:?}" "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}"

# Credentials via the environment: never in a config file, never in `ps`.
export RCLONE_S3_PROVIDER=Cloudflare
export RCLONE_S3_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_S3_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_S3_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_S3_NO_CHECK_BUCKET=true
# rclone 1.60 (Ubuntu's package) verifies each upload with a follow-up
# request R2 answers "501 Not Implemented", failing every first attempt.
# Skip it — `put` below verifies the uploaded size itself instead.
export RCLONE_S3_NO_HEAD=true
# Everything is configured from the environment; there is no config file.
export RCLONE_CONFIG=/dev/null
REMOTE=":s3:${R2_BUCKET}"

case "${1:-}" in
  ls)     rclone lsf "$REMOTE" --include "secondmind-*.dump.age" | sort ;;
  latest) rclone lsf "$REMOTE" --include "secondmind-*.dump.age" | sort | tail -1 ;;
  cat)    rclone cat "${REMOTE}/${2:?name}" ;;
  put)
    rclone copyto "${2:?file}" "${REMOTE}/${3:?name}"
    local_size=$(wc -c < "$2" | tr -d " ")
    remote_size=$(rclone lsf --format s "${REMOTE}/$3")
    if [ "$local_size" != "$remote_size" ]; then
      echo "Upload check failed: local ${local_size} bytes, R2 has ${remote_size:-nothing}." >&2
      exit 1
    fi
    echo "verified ${remote_size} bytes in R2"
    ;;
  prune)  rclone delete "$REMOTE" --min-age "${2:?days}d" --include "secondmind-*.dump.age" ;;
  *)      echo "usage: $0 ls|latest|cat <name>|put <file> <name>|prune <days>" >&2; exit 2 ;;
esac
