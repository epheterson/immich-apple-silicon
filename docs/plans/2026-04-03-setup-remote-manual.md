# Setup: Remote Immich + Manual Config

> **Historical design doc (2026-04-03).** Captures the original design thinking; not maintained as the product evolves. For current, accurate documentation see [README.md](../../README.md) and [docs/deployment.md](../deployment.md).

## Problem

Setup auto-discovers Docker on localhost. Users with NAS+Mac splits (Docker on NAS, compute on Mac) can't use it. Docker shouldn't be required on the Mac at all.

## Design

Three setup modes sharing one config format (`~/.immich-accelerator/config.json`).

### Mode 1: Local Docker (existing)

```bash
python -m accelerator setup
```

Auto-discovers Immich from local Docker. Unchanged from current behavior.

### Mode 2: Remote Immich

```bash
python -m accelerator setup --url http://nas:2283 --api-key <key>
```

- Queries Immich API (`/api/server/version`, `/api/server/config`) for version
- Prompts interactively for DB and Redis connection details:
  - DB host, port, username, password, database name
  - Redis host, port
  - Upload/media path (as seen from the Mac — NFS/SMB mount point)
- Server extract:
  - If Docker CLI available on Mac: `docker pull ghcr.io/immich-app/immich-server:<version>`, create temp container, `docker cp`, remove container
  - If no Docker: print a one-liner for the user to run on their NAS, plus instructions for where to put the result

### Mode 3: Manual

```bash
python -m accelerator setup --manual
```

- Creates `~/.immich-accelerator/config.json` template with all fields and comments
- Prints instructions for server extraction
- User edits config, runs `accelerator start`

### Server extraction without local Docker

When Docker isn't on the Mac, print:

```
Run this on your NAS (where Docker runs):
  docker cp immich_server:/usr/src/app/server - | gzip > /tmp/immich-server.tar.gz
  docker cp immich_server:/build - | gzip > /tmp/immich-build.tar.gz

Copy both to your Mac:
  scp nas:/tmp/immich-server.tar.gz ~/.immich-accelerator/
  scp nas:/tmp/immich-build.tar.gz ~/.immich-accelerator/

Then run:
  python -m accelerator setup --import-server
```

The `--import-server` flag unpacks the tarballs, rebuilds Sharp for macOS, and finishes setup.

### Config format

No changes to the config schema. Same fields, all modes produce the same config.json. Adding `api_key` field (already in v1.1.0 branch).

### Validation

All modes run the same post-setup checks:
- DB connectivity (can we reach Postgres?)
- Redis connectivity
- ffmpeg encoder validation (libwebp, videotoolbox)
- ML service detection
- Server files present and valid

### What this unblocks

- flsabourin's NAS+Mac setup (issue #2)
- Any user who doesn't want Docker on their Mac
- Users who want full control over config
