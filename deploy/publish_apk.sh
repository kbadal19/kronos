#!/usr/bin/env bash
# Publish the latest Android build to the server's download link.
#   ./publish_apk.sh root@<server>
# Prints the link. The token comes from APP_DOWNLOAD_TOKEN in the server's
# backend/.env (created on first run).
set -euo pipefail

TARGET="${1:?usage: ./publish_apk.sh user@host}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APK="$HERE/../mobile/android/app/build/outputs/apk/release/app-release.apk"
[ -f "$APK" ] || { echo "No release build at $APK — build it first." >&2; exit 1; }

ssh "$TARGET" 'mkdir -p /opt/secondmind/downloads && cd /opt/secondmind/backend && \
  (grep -q "^APP_DOWNLOAD_TOKEN=." .env || printf "\nAPP_DOWNLOAD_TOKEN=%s\n" "$(head -c 24 /dev/urandom | base64 | tr -d "/+=")" >> .env)'
# Atomic swap: a phone mid-download keeps reading the old file.
scp -q "$APK" "$TARGET:/opt/secondmind/downloads/.krono.apk.new"
ssh "$TARGET" 'mv /opt/secondmind/downloads/.krono.apk.new /opt/secondmind/downloads/krono.apk'

HOST="$(ssh "$TARGET" 'grep "^ALLOWED_HOSTS=" /opt/secondmind/backend/.env | cut -d= -f2 | cut -d, -f1')"
TOKEN="$(ssh "$TARGET" 'grep "^APP_DOWNLOAD_TOKEN=" /opt/secondmind/backend/.env | cut -d= -f2')"
echo "https://${HOST}/app/${TOKEN}/krono.apk"
