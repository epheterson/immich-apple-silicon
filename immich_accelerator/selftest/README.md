# Self-test fixtures

Tiny inputs for `selftest.mjs`, which runs Immich's own code on them with the worker's environment. See `docs/plans/2026-10-09-full-selftest.md`.

| File | What it is | Expected |
|---|---|---|
| `face.jpg` | 192x240 JPEG of Neil Armstrong, NASA, 1969 (public domain, via Wikimedia Commons), with EXIF Make `Accelerator`, Model `Selftest`, taken 2024-05-17 10:30:00 | decodes at 192x240, metadata reads back, one face, a CLIP embedding |
| `iphone.heic` | 96x64 HEVC HEIC made with `sips` | decodes at 96x64 (only through the HEIC shim) |
| `hdr.mp4` | 1 s 192x108 HEVC Main 10, BT.2020 PQ, with a mono AAC tone | probes as 10-bit HDR; thumbnails; transcodes to SDR H.264 with audio |
| `ocr.jpg` | 360x120 card reading `IMMICH 2026` | OCR reads `IMMICH` |
