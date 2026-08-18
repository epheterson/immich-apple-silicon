# Security

## Reporting a vulnerability

Please report security issues privately through [GitHub's advisory form](https://github.com/epheterson/immich-apple-silicon/security/advisories/new) rather than opening a public issue.

Include what you found, how to reproduce it, and what an attacker could do with it. You will get a reply within a few days.

## What this software does to your machine

[docs/security.md](docs/security.md) documents every network-facing surface, every file the accelerator writes outside its own directory, and how to undo each one. Worth reading before you install it, and worth checking if you are auditing a report.

The short version: the accelerator runs Immich's own microservices worker natively, binds the ML service to a local port, writes to `~/.immich-accelerator`, and creates one synthetic symlink for `/build` if Immich needs it. It does not phone home.

## Supported versions

Fixes go into the current release. There are no long-term support branches, so please upgrade before reporting a problem with an older version.
