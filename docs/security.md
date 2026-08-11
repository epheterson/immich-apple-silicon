# Safety and security

## Safety guarantees

- **Immich's Docker image is unmodified.** No custom images, no patches.
- **The native worker runs Immich's own code.** Extracted from the Docker image, not reimplemented.
- **UPSERT-safe database writes.** The native worker uses Immich's own job pipeline with the same UPSERT logic.
- **Version-matched.** The extracted server always matches the Docker image version exactly.

See [../README.md#what-we-modify-and-how-to-undo-it](../README.md#what-we-modify-and-how-to-undo-it) for exactly what's changed on disk and how to revert it.

## Network-facing surfaces

| Surface | Default bind | Auth | Notes |
|---------|--------------|------|-------|
| Config file (`~/.immich-accelerator/config.json`) | n/a (local file) | filesystem | chmod 600 |
| Postgres | `127.0.0.1:5432` | Postgres auth | localhost only by default |
| Redis | `127.0.0.1:6379` | none (Redis default) | localhost only by default |
| Dashboard | `0.0.0.0:8420` | none | LAN-accessible. The Re-queue button triggers job processing via the Immich API — see [usage.md#dashboard](usage.md#dashboard) |
| ML service (port 3003) | `0.0.0.0:3003` | none | LAN-accessible, same trust model as Immich's own Docker `machine-learning` container — see [deployment.md#security-note](deployment.md#security-note) |

If you're on an untrusted network, put the dashboard and/or ML port behind a firewall or VPN rather than exposing them to the internet, or bind them to localhost only.
