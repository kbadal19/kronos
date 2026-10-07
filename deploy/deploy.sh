#!/usr/bin/env bash
# Deploy to an Ubuntu server.
#   ./deploy.sh root@<server-ip>                    # dedicated box, own Caddy
#   COMPOSE_FILE=docker-compose.shared.yml \
#     ./deploy.sh root@<server-ip>                  # behind another project's proxy
#
# backend/.env is never uploaded: production secrets live only on the
# server at /opt/secondmind/backend/.env (create it once, chmod 600). The
# local file is a dev config — DEBUG on, no DB password — and shipping it
# would quietly replace the server's.
#
# Copies backend/ and deploy/ up, builds the image there, and brings up
# Postgres + Redis + the worker + Caddy. No database restore step: unlike
# masterflow this starts empty, and after the first deploy the only thing
# you should ever restore from is an encrypted backup (see restore.sh).
set -euo pipefail

TARGET="${1:?usage: ./deploy.sh user@host}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
COMPOSE="docker compose -f $COMPOSE_FILE --env-file ../backend/.env"

echo "==> Installing Docker on the server (no-op if present)"
ssh "$TARGET" 'command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh)'

if [ "$COMPOSE_FILE" = "docker-compose.prod.yml" ]; then
  echo "==> Hardening: firewall to 22/80/443 only"
  # Postgres and Redis publish no ports in the prod compose file, but a
  # misconfiguration shouldn't be one typo away from exposing everything you
  # have ever thought. Skipped on a shared box: its firewall belongs to
  # whoever else lives there.
  ssh "$TARGET" 'command -v ufw >/dev/null && (ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable) || true'
fi

echo "==> Copying code"
ssh "$TARGET" 'mkdir -p /opt/secondmind'
rsync -az --delete \
  --exclude venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude spool --exclude staticfiles --exclude .git \
  --exclude .env --exclude entities.txt --exclude knowledge \
  "$ROOT/backend/" "$TARGET:/opt/secondmind/backend/"
rsync -az "$HERE/" "$TARGET:/opt/secondmind/deploy/"

if ! ssh "$TARGET" 'test -f /opt/secondmind/backend/.env'; then
  echo "/opt/secondmind/backend/.env is missing on the server — create it from backend/.env.example first." >&2
  exit 1
fi

echo "==> Building and starting ($COMPOSE_FILE)"
ssh "$TARGET" "cd /opt/secondmind/deploy && $COMPOSE up -d --build"

echo "==> Done."
echo
echo "Create your one and only user:"
echo "    ssh $TARGET 'cd /opt/secondmind/deploy && $COMPOSE exec backend python manage.py createsuperuser'"
echo
echo "Follow the backend logs:"
echo "    ssh $TARGET 'cd /opt/secondmind/deploy && $COMPOSE logs -f backend'"
