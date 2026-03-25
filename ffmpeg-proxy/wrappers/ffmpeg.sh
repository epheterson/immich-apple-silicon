#!/bin/sh
# ffmpeg wrapper — proxies calls to the native macOS ffmpeg-proxy service.
# Bind-mounted over /usr/bin/ffmpeg inside the Immich Docker container.
# OrbStack: host.internal | Docker Desktop: host.docker.internal
PROXY="${FFMPEG_PROXY_URL:-http://host.internal:3005/ffmpeg}"
FALLBACK="/usr/lib/jellyfin-ffmpeg/ffmpeg"
TMPF=$(mktemp)
trap 'rm -f "$TMPF"' EXIT

# Build JSON safely using Node.js (handles all special chars properly)
ARGS_JSON=$(node -e "process.stdout.write(JSON.stringify(process.argv.slice(1)))" -- "$@")
curl -s --max-time 610 -X POST "$PROXY" -H "Content-Type: application/json" \
  -d "{\"args\":$ARGS_JSON}" -o "$TMPF" 2>/dev/null

# Parse response via Node.js; fall back to native ffmpeg if proxy unreachable
if [ ! -s "$TMPF" ]; then
  [ -x "$FALLBACK" ] && exec "$FALLBACK" "$@"
  exit 1
fi

node -e "
const d=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));
if(d.stdout)process.stdout.write(d.stdout);
if(d.stderr)process.stderr.write(d.stderr);
process.exit(d.returncode||0);
" "$TMPF"
