#!/bin/sh
PROXY="http://host.internal:3005/ffmpeg"
TMPF=$(mktemp)
JSON="["
F=1
for a in "$@"; do
  e=$(printf '%s' "$a" | sed 's/\\/\\\\/g; s/"/\\"/g')
  [ "$F" = "1" ] && JSON="$JSON\"$e\"" && F=0 || JSON="$JSON,\"$e\""
done
JSON="$JSON]"
curl -s --max-time 610 -X POST "$PROXY" -H "Content-Type: application/json" -d "{\"args\":$JSON}" -o "$TMPF" 2>/dev/null
node -e "
const d=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));
require('fs').unlinkSync(process.argv[1]);
if(d.stdout)process.stdout.write(d.stdout);
if(d.stderr)process.stderr.write(d.stderr);
process.exit(d.returncode||0);
" "$TMPF"
