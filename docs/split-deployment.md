# Split deployment (NAS + Mac, or any two hosts)

Run Immich's Docker stack (API, Postgres, Redis) on one host and the native
accelerator (microservices worker + ML) on another. This is the setup most
people want: keep the database where it already lives, add Apple Silicon compute
on a Mac.

## The one thing you have to get right

**Both machines need to see the same files at the same absolute paths, via a shared filesystem.** The native worker reads and writes directly to disk — there is no HTTP transport of thumbnails between machines. If the NAS mounts `/volume1/photos` and the Mac mounts that same share over SMB/NFS at `/Volumes/photos`, you now have two different absolute paths for the same bytes, and Immich's database will only know one of them.

You need one absolute path that resolves to the same files on both sides. See "Two ways to get there" below.

## Topology

1. **On the NAS (or wherever Docker lives)**: Immich Docker runs server (API-only), Postgres, and Redis. Expose Postgres and Redis on the LAN (not just localhost).
2. **On the Mac**: The accelerator runs the microservices worker and ML service. Setup pulls the Immich server directly from ghcr.io — no Docker required on the Mac.

```bash
immich-accelerator setup --url http://nas:2283 --api-key YOUR_KEY
```

## Two ways to get there

**Option A — match the Mac's path inside Docker** (recommended for new installs).

Mount your Mac's shared-filesystem path on both sides with the same absolute path. Say the Mac mounts the NAS share at `/Volumes/photos`:

```yaml
# NAS docker-compose — bind the storage to the same absolute path Docker uses
volumes:
  - /volume1/photos:/Volumes/photos
environment:
  - IMMICH_MEDIA_LOCATION=/Volumes/photos
```

Docker writes `/Volumes/photos/...` to Postgres. The Mac worker opens the exact same path via its SMB/NFS mount. Same bytes, same path.

**Option B — match Docker's path on the Mac** (zero Docker changes).

Use a macOS [synthetic link](https://man.cx/synthetic.conf(5)) to make the Mac resolve Docker's internal path to your local mount:

```bash
# /etc/synthetic.d/immich-accelerator
data	Volumes/photos/immich/library
```

Reboot. Now `/data` on the Mac resolves to the SMB/NFS mount, matching what Docker already stores in the database. No `IMMICH_MEDIA_LOCATION` change needed.

> **Synthetic links can only create a single top-level name** (e.g. `/data`, `/immich`) — that's a macOS limitation, not ours. If Docker is using the container default `IMMICH_MEDIA_LOCATION=/usr/src/app/upload`, you **cannot** mirror that path on the Mac (you can't synthesize `/usr/src/app/...`, and `/usr` already exists). Use Option A, or first set `IMMICH_MEDIA_LOCATION` to a top-level path like `/data` and then synthesize that.

## Fresh split deployment: let the frontend initialize geodata first

If your Immich frontend has run **api-only from the very start** (`IMMICH_WORKERS_INCLUDE=api` set before it ever ran its own microservices worker), the reverse-geocoding tables were never initialized. The accelerator then becomes the first microservices worker to touch the database and tries to run Immich's one-time **geodata import** — a large bulk insert that can break over a network database connection (`write EPIPE`), so the worker fails to start.

Fix: initialize geodata once on the frontend, then hand off to the accelerator.

1. On the frontend, temporarily **remove** `IMMICH_WORKERS_INCLUDE=api` (or set it to include the microservices worker) and restart it.
2. Wait for it to finish the geodata import (watch its logs for "geodata import" completing).
3. **Re-add** `IMMICH_WORKERS_INCLUDE=api` and restart the frontend.
4. Start the accelerator — the tables already exist, so it skips the import.

This only affects brand-new split installs. Once geodata is initialized it stays initialized, and normal upgrades are unaffected.

## Changing IMMICH_MEDIA_LOCATION on an existing install

Immich automatically rewrites all file paths in the database on restart when `IMMICH_MEDIA_LOCATION` changes. It's safe — **but back up your database first**.
