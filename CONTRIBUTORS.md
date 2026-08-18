# Contributors

This project is better than it would have been because of the people below. Thank you.

## Code

### [@lesurJ](https://github.com/lesurJ)

- The whole SigLIP and SigLIP2 catalog running on the native MLX path ([#123](https://github.com/epheterson/immich-apple-silicon/pull/123))
- Found that most CLIP models were being fed the wrong pixels, and fixed the preprocessing ([#122](https://github.com/epheterson/immich-apple-silicon/pull/122))
- `--ml-only` mode, so a spare Mac can be a network ML node ([#119](https://github.com/epheterson/immich-apple-silicon/pull/119))
- Live ML logs ([#121](https://github.com/epheterson/immich-apple-silicon/pull/121)), onnxruntime threading ([#118](https://github.com/epheterson/immich-apple-silicon/pull/118)), and the docs restructure ([#128](https://github.com/epheterson/immich-apple-silicon/pull/128))

### [@pl4za](https://github.com/pl4za)

- Found HEIC decoding blocking the worker's event loop, which was failing thumbnails permanently on NAS libraries ([#125](https://github.com/epheterson/immich-apple-silicon/pull/125))
- QuickLook fallback for video that ffmpeg's HEVC decoder rejects ([#126](https://github.com/epheterson/immich-apple-silicon/pull/126))
- Job retries that survive a long outage ([#129](https://github.com/epheterson/immich-apple-silicon/pull/129))
- Diagnosed a NAS share vanishing mid-session, which became the accelerator handling its own mounts ([#130](https://github.com/epheterson/immich-apple-silicon/pull/130))
- Dashboard progress fix ([#131](https://github.com/epheterson/immich-apple-silicon/pull/131))

### [@RxChi1d](https://github.com/RxChi1d)

- Found that a Docker install whose daemon is not running crashed the accelerator instead of falling back ([#138](https://github.com/epheterson/immich-apple-silicon/pull/138))
- Reported the native ML engine holding models after jobs finish, with a fix ([#136](https://github.com/epheterson/immich-apple-silicon/issues/136), [#137](https://github.com/epheterson/immich-apple-silicon/pull/137))

## Reports that changed the code

Every issue below is cited in a source comment explaining why some piece of this
project works the way it does. Finding the problem is most of the work.

- [@shtefko](https://github.com/shtefko) — split-deployment timeouts, `stop` and `restart` not stopping anything, motion video, Canon RAW ([#74](https://github.com/epheterson/immich-apple-silicon/issues/74), [#80](https://github.com/epheterson/immich-apple-silicon/issues/80), [#81](https://github.com/epheterson/immich-apple-silicon/issues/81), [#95](https://github.com/epheterson/immich-apple-silicon/issues/95), [#99](https://github.com/epheterson/immich-apple-silicon/issues/99))
- [@jhoogeboom](https://github.com/jhoogeboom) — pg_dump, the ML directory going stale after an upgrade, thumbnails, ML errors ([#19](https://github.com/epheterson/immich-apple-silicon/issues/19), [#20](https://github.com/epheterson/immich-apple-silicon/issues/20), [#24](https://github.com/epheterson/immich-apple-silicon/issues/24), [#29](https://github.com/epheterson/immich-apple-silicon/issues/29))
- [@flsabourin](https://github.com/flsabourin) — stalled thumbnails, and the ML crash that still pins our mlx version ([#33](https://github.com/epheterson/immich-apple-silicon/issues/33), [#38](https://github.com/epheterson/immich-apple-silicon/issues/38))
- [@Rustymage](https://github.com/Rustymage) — upload path mapping, and Neural Engine progress reporting ([#61](https://github.com/epheterson/immich-apple-silicon/issues/61), [#68](https://github.com/epheterson/immich-apple-silicon/issues/68))
- [@KoenM9264](https://github.com/KoenM9264) — the file-descriptor leak that crashed video encoding ([#89](https://github.com/epheterson/immich-apple-silicon/issues/89))
- [@goldhandconsultancy](https://github.com/goldhandconsultancy) — setup not detecting the upload location ([#62](https://github.com/epheterson/immich-apple-silicon/issues/62))
- [@xobust](https://github.com/xobust) — support for a separate thumbnail location ([#115](https://github.com/epheterson/immich-apple-silicon/issues/115))
- [@exkuretrol](https://github.com/exkuretrol) — authenticated Redis ([#56](https://github.com/epheterson/immich-apple-silicon/issues/56))
- [@pwnmeow](https://github.com/pwnmeow) — UHDR JPEG handling ([#44](https://github.com/epheterson/immich-apple-silicon/issues/44))
- [@Amoyblack](https://github.com/Amoyblack) — path mapping ([#42](https://github.com/epheterson/immich-apple-silicon/issues/42))
- [@kg6kvq](https://github.com/kg6kvq) — path changes breaking the server ([#43](https://github.com/epheterson/immich-apple-silicon/issues/43))

---

Contributions are welcome, large or small. A bug report with enough detail to
reproduce is worth as much as a patch. See [CONTRIBUTING.md](CONTRIBUTING.md) to
get started.
