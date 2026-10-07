#!/usr/bin/env bash
# Prove a backup restores — without touching production. Run from your Mac.
#   ./verify_backup.sh root@<server>              newest backup in R2
#   ./verify_backup.sh root@<server> <name>       a specific one
#   ./verify_backup.sh --file <path.dump.age>     a local encrypted dump
#
# Decrypts here with your private key, restores into a throwaway local
# database, prints what came back, and drops it. Run it after setup and
# then every month or so: the failure it catches is silent — a nightly
# upload that has been unrestorable for months.
set -euo pipefail

KEY_FILE="${AGE_KEY_FILE:-$HOME/.age/secondmind.key}"
SCRATCH="secondmind_restore_check"

if [ "${1:-}" = "--file" ]; then
  SOURCE="cat ${2:?path}"
  LABEL="$2"
else
  TARGET="${1:?usage: ./verify_backup.sh user@host [name] | --file path}"
  NAME="${2:-$(ssh "$TARGET" 'cd /opt/secondmind/deploy && ./r2.sh latest')}"
  [ -n "$NAME" ] || { echo "No backups found in R2." >&2; exit 1; }
  SOURCE="ssh $TARGET 'cd /opt/secondmind/deploy && ./r2.sh cat $NAME'"
  LABEL="$NAME"
fi

echo "==> Restoring ${LABEL} into a scratch database"
dropdb --if-exists "$SCRATCH"
createdb "$SCRATCH"
trap 'dropdb --if-exists "$SCRATCH"' EXIT
psql -q -d "$SCRATCH" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

eval "$SOURCE" | age -d -i "$KEY_FILE" | pg_restore -d "$SCRATCH" --no-owner --exit-on-error

echo "==> What came back:"
psql -d "$SCRATCH" -At -F ' ' -c "
  select 'captures',    count(*)::text from capture_capture union all
  select 'transcripts', count(*)::text from capture_transcript union all
  select 'entities',    count(*)::text from graph_node union all
  select 'edges',       count(*)::text from graph_edge union all
  select 'corrections', count(*)::text from pipeline_correctionpair union all
  select 'newest note', coalesce(max(captured_at)::text, '-') from capture_capture"
echo "==> Backup restores cleanly."
