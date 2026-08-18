# Contributors

This project is better than it would have been because of the people below. Thank you.

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

---

Contributions are welcome, large or small. Bug reports with enough detail to reproduce are worth as much as patches. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.
