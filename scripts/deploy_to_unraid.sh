#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 UNRAID_USER UNRAID_HOST [REMOTE_DIR]" >&2
  exit 1
fi

UNRAID_USER="$1"
UNRAID_HOST="$2"
REMOTE_DIR="${3:-/mnt/user/appdata/storyforge}"
TMP_ARCHIVE="/tmp/storyforge.tar.gz"

cd "$(dirname "$0")/.."

echo "Creating archive..."
tar --exclude='.venv' --exclude='logs' --exclude='temp' --exclude='__pycache__' -czf "$TMP_ARCHIVE" \
  app config Dockerfile docker-compose.yml requirements.txt .env.example README.md

echo "Creating target directories on Unraid..."
ssh "${UNRAID_USER}@${UNRAID_HOST}" 'mkdir -p /mnt/user/appdata/storyforge /mnt/user/appdata/storyforge/logs /mnt/user/appdata/storyforge/temp /mnt/user/Books/EPUB "/mnt/user/Books/Audiobooks/Test Output"'

echo "Uploading archive..."
scp "$TMP_ARCHIVE" "${UNRAID_USER}@${UNRAID_HOST}:/tmp/storyforge.tar.gz"

echo "Extracting archive on Unraid..."
ssh "${UNRAID_USER}@${UNRAID_HOST}" "mkdir -p '$REMOTE_DIR' && tar -xzf /tmp/storyforge.tar.gz -C '$REMOTE_DIR' --strip-components=0"

echo "Deployment complete."
