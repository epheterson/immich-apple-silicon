#!/bin/sh
# ffprobe wrapper — proxies calls to the native macOS ffmpeg-proxy service.
# Bind-mounted over /usr/bin/ffprobe inside the Immich Docker container.
# OrbStack: host.internal | Docker Desktop: host.docker.internal
PROXY="${FFPROBE_PROXY_URL:-http://host.internal:3005/ffprobe}"
FALLBACK="/usr/lib/jellyfin-ffmpeg/ffprobe"
TMPF=$(mktemp)
trap 'rm -f "$TMPF"' EXIT

# Build JSON safely using Node.js (handles all special chars properly)
ARGS_JSON=$(node -e "process.stdout.write(JSON.stringify(process.argv.slice(1)))" -- "$@")
curl -s --max-time 30 -X POST "$PROXY" -H "Content-Type: application/json" \
  -d "{\"args\":$ARGS_JSON}" -o "$TMPF" 2>/dev/null

# Parse response via Node.js; fall back to native ffprobe if proxy unreachable
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
