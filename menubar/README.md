# Immich Accelerator menu bar app

A native SwiftUI menu-bar app for the accelerator: health at a glance, daily
actions one click away. Minimal, informative, useful, complete.

- **Status**: worker, ML (with a NATIVE/PYTHON engine badge), dashboard, versions.
  Read straight from the accelerator's own truth: pidfiles, the ML service's
  `/ping`, `config.json`, and the installed `VERSION`. No extra daemons.
- **Actions**: start / stop / restart (brew services), run the real `ml-test`
  with the result inline, open Immich, open the dashboard, open logs.
- **Launch at Login** toggle (ServiceManagement).
- The menu-bar bolt reflects health: filled = running, slash = stopped,
  exclamation = degraded.

Design inspired by [Immich-Accelerator-Helper](https://github.com/pl4za/Immich-Accelerator-Helper)
by [@pl4za](https://github.com/pl4za) (MIT). Implemented fresh on this repo's stack.

## Build

```bash
cd menubar && bash scripts/build_app.sh 1.6.0
open "Immich Accelerator.app"
```

Produces an ad-hoc-signed `Immich Accelerator.app` (~360 KB, LSUIElement), the
same no-notarization distribution pattern as the native ML bundle. macOS 14+.

## Headless verification

The binary doubles as a status probe, so CI or ssh can validate the exact state
logic the panel renders without a GUI session:

```bash
./.build/release/AcceleratorBar status
# version=1.6.0
# worker=true
# ml_up=true ml_healthy=true engine=NATIVE
# dashboard=true
# immich=3.0.2 url=http://10.0.0.14:2283
# overall=Running
```
