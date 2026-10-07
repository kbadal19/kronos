#!/usr/bin/env bash
# Restore an encrypted R2 backup into the LIVE database. Run from your Mac.
#   ./restore.sh root@<server> secondmind-20261005T030000Z.dump.age [key-file]
#
# This overwrites production (--clean). To check a backup without touching
# anything, use verify_backup.sh instead.
#
# The private key never leaves this machine: the encrypted dump streams
# down from the server, is decrypted here, and streams back up into
# pg_restore.
set -euo pipefail

TARGET="${1:?usage: ./restore.sh user@host <backup-name> [age-key-file]}"
NAME="${2:?usage: ./restore.sh user@host <backup-name> [age-key-file]}"
KEY_FILE="${3:-$HOME/.age/secondmind.key}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.shared.yml}"
REMOTE_DIR=/opt/secondmind/deploy

read -r -p "Overwrite the live database on ${TARGET} with ${NAME}? Type 'restore' to continue: " answer
[ "$answer" = "restore" ] || { echo "Aborted."; exit 1; }

echo "==> Streaming ${NAME} down, decrypting locally, restoring"
ssh "$TARGET" "cd $REMOTE_DIR && ./r2.sh cat $NAME" \
  | age -d -i "$KEY_FILE" \
  | ssh "$TARGET" "cd $REMOTE_DIR && set -a && . ../backend/.env && set +a && \
      docker compose -f $COMPOSE_FILE --env-file ../backend/.env exec -T postgres \
        pg_restore -U \"\$DB_USER\" -d \"\$DB_NAME\" --no-owner --clean --if-exists"

echo "==> Restored. Check the counts:"
echo "    ssh $TARGET 'cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE --env-file ../backend/.env exec backend python manage.py shell -c \"from capture.models import Capture; print(Capture.objects.count())\"'"
