# Deployment modes in depth

Full detail behind the deployment modes in the README's [How it works](../README.md#how-it-works) section.

## Split deployment — worker + ML on a remote host (NAS + Mac, or any two hosts)

Run Immich's Docker stack (API, Postgres, Redis) on one host and the native
accelerator (microservices worker + ML) on another. This is the setup most
people want: keep the database where it already lives, add Apple Silicon compute
on a Mac.

### The one thing you have to get right

**Both machines need to see the same files at the same absolute paths, via a shared filesystem.** The native worker reads and writes directly to disk. There is no HTTP transport of thumbnails between machines. If the NAS mounts `/volume1/photos` and the Mac mounts that same share over SMB/NFS at `/Volumes/photos`, you now have two different absolute paths for the same bytes, and Immich's database will only know one of them.

You need one absolute path that resolves to the same files on both sides. See "Two ways to get there" below.

If you only want remote ML compute — not the worker — you don't need any of this: see [ML-only network node](#ml-only-network-node) below instead.

### What makes an install "split"

The `immich_url` key in `~/.immich-accelerator/config.json`. `setup --url` is the only thing that writes it, and it means the Immich at that address is the one this install answers to.

That matters if the Mac also runs Immich in Docker for something unrelated. The accelerator will still use a local image as a place to fetch server files from, but it does not read configuration out of the container: that container is not necessarily your server, and its database settings are not yours. Your version comes from the Immich you configured.

On a local install there is no `immich_url`, the container on this Mac is the server, and its `IMMICH_WORKERS_INCLUDE` and `IMMICH_MEDIA_LOCATION` are checked before the worker starts.

### Topology

1. **On the NAS (or wherever Docker lives)**: Immich Docker runs server (API-only), Postgres, and Redis. Expose Postgres and Redis on the LAN (not just localhost).
2. **On the Mac**: The accelerator runs the microservices worker and ML service. Setup pulls the Immich server directly from ghcr.io, no Docker required on the Mac.

```bash
immich-accelerator setup --url http://nas:2283 --api-key YOUR_KEY
```

### Two ways to get there

**Option A: match the Mac's path inside Docker** (recommended for new installs).

Mount your Mac's shared-filesystem path on both sides with the same absolute path. Say the Mac mounts the NAS share at `/Volumes/photos`:

```yaml
# NAS docker-compose: bind the storage to the same absolute path Docker uses
volumes:
  - /volume1/photos:/Volumes/photos
environment:
  - IMMICH_MEDIA_LOCATION=/Volumes/photos
```

Docker writes `/Volumes/photos/...` to Postgres. The Mac worker opens the exact same path via its SMB/NFS mount. Same bytes, same path.

**Option B: match Docker's path on the Mac** (zero Docker changes).

Use a macOS [synthetic link](https://man.cx/synthetic.conf(5)) to make the Mac resolve Docker's internal path to your local mount:

```bash
# /etc/synthetic.d/immich-accelerator
data	Volumes/photos/immich/library
```

Reboot. Now `/data` on the Mac resolves to the SMB/NFS mount, matching what Docker already stores in the database. No `IMMICH_MEDIA_LOCATION` change needed.

> **Synthetic links can only create a single top-level name** (e.g. `/data`, `/immich`); that's a macOS limitation, not ours. If Docker is using the container default `IMMICH_MEDIA_LOCATION=/usr/src/app/upload`, you **cannot** mirror that path on the Mac (you can't synthesize `/usr/src/app/...`, and `/usr` already exists). Use Option A, or first set `IMMICH_MEDIA_LOCATION` to a top-level path like `/data` and then synthesize that.

### Fresh split deployment: let the frontend initialize geodata first

If your Immich frontend has run **api-only from the very start** (`IMMICH_WORKERS_INCLUDE=api` set before it ever ran its own microservices worker), the reverse-geocoding tables were never initialized. The accelerator then becomes the first microservices worker to touch the database and tries to run Immich's one-time **geodata import**, a large bulk insert that can break over a network database connection (`write EPIPE`), so the worker fails to start.

Fix: initialize geodata once on the frontend, then hand off to the accelerator.

1. On the frontend, temporarily **remove** `IMMICH_WORKERS_INCLUDE=api` (or set it to include the microservices worker) and restart it.
2. Wait for it to finish the geodata import (watch its logs for "geodata import" completing).
3. **Re-add** `IMMICH_WORKERS_INCLUDE=api` and restart the frontend.
4. Start the accelerator; the tables already exist, so it skips the import.

This only affects brand-new split installs. Once geodata is initialized it stays initialized, and normal upgrades are unaffected.

### Changing IMMICH_MEDIA_LOCATION on an existing install

Immich automatically rewrites all file paths in the database on restart when `IMMICH_MEDIA_LOCATION` changes. It's safe, **but back up your database first**.

## ML-only network node

This is the `worker off, ml on` case in [Choosing what runs](usage.md#choosing-what-runs), with a setup flag (`--ml-only`) that skips every Docker and database step.

Dedicate a spare Apple Silicon Mac to ML compute only — no worker, no Docker,
no Postgres, no Redis, no library mount — and point another Immich instance's
stock **Administration → Machine Learning Settings → Remote Machine Learning
URL** at it:

```text
http://<this-mac-ip>:3003
```

This turns the Mac into pure ML compute for an Immich instance (or several)
running anywhere else on the network, without the shared-filesystem/path-alignment
rules that [split deployment](#split-deployment--worker--ml-on-a-remote-host-nas--mac-or-any-two-hosts)
requires — because unlike the worker, the ML service never touches the
library on disk, only the image bytes and text sent to it over HTTP.

### Setup

```bash
immich-accelerator setup --ml-only
```

Finds (or offers to build) the Python venv fallback engine, writes a minimal config
(`"worker": false`), and offers to start now and install as a launch-at-login service —
same as a full install, minus every Docker/worker/database step. To turn this
node into a full install later, re-run `immich-accelerator setup`: the worker
needs the Docker, database and library details that ML-only setup never collects,
so `component worker on` cannot conjure them and will tell you so.

### What's different from a full install

- `start` / `watch` / `brew services start` bring up only the ML engine (native Swift by
  default, Python venv fallback) and the [dashboard](usage.md#dashboard) — no worker, no
  `/build` link, no Immich version tracking, no database connectivity checks.
- `status`, `stop`, `logs ml`, and `ml-test` all work exactly as in a full install.
- The dashboard's processing-progress bars aren't meaningful here — there's no single
  "owning" Immich library, since this node can serve any number of Immich instances —
  so it shows ML health and Apple Silicon hardware utilization only.
- Changing the CLIP/face model is still controlled entirely from each consuming
  Immich instance's own admin settings, same as any other deployment — see
  [ml-engine.md](ml-engine.md).

### Security note

The ML service binds `0.0.0.0:3003` (LAN-accessible) and, like Immich's own Docker ML
service, has **no authentication** — anything that can reach the port can submit
inference requests. This is the same trust model as Immich's stock `machine-learning`
container; ml-only mode just makes it reachable from other hosts instead of only
`localhost`. If you're on an untrusted network, put it behind a firewall or VPN and
don't expose port 3003 to the internet — see [security.md](security.md) for the
accelerator's other network-facing surfaces.
