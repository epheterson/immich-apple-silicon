# Troubleshooting

The accelerator will tell you what's wrong. Click a symptom below for the fix.

<details>
<summary><b>Setup says "Upload: not detected"</b></summary>

Symptom: `immich-accelerator setup` finds your Immich container but reports `Upload: not detected`.

Cause: fixed in v1.5.8. Older versions only recognized uploads mounted under a `/upload` path; the modern Immich compose mounts `${UPLOAD_LOCATION}:/data` and leaves `IMMICH_MEDIA_LOCATION` unset, so detection missed it.

Fix: `brew upgrade immich-accelerator` and re-run setup. If you're on a same-machine Docker Desktop setup where the container path (`/data`) differs from the host mount, the absolute paths still have to match for the native worker to read them (see [deployment.md](deployment.md#split-deployment--worker--ml-on-a-remote-host-nas--mac-or-any-two-hosts)); the simplest fix is to set `IMMICH_MEDIA_LOCATION` (and the bind mount) to the host path so both sides agree.

</details>

<details>
<summary><b>Thumbnails 404 in the Immich web UI</b></summary>

Symptom: the native worker runs happily, but Immich's API server logs `ENOENT: /data/thumbs/.../xxx_thumbnail.webp` and thumbnails never show up.

Cause: split-setup path mismatch. Docker Immich stores absolute paths like `/data/library/<uuid>/...` in Postgres; the native worker writes to your `upload_mount` which is something else. Docker API then 404s the stored path.

Fix: run `immich-accelerator setup --url http://your-nas:2283 --api-key YOUR_KEY` again. v1.4.1+ detects Docker's media root via the API and refuses to save a broken config. You'll see the mismatch explicitly with both walkthroughs (match Docker, or synthetic link on Mac). See [deployment.md](deployment.md#split-deployment--worker--ml-on-a-remote-host-nas--mac-or-any-two-hosts) for the two options.

</details>

<details>
<summary><b>Microservices red after editing <code>/etc/synthetic.d/immich-accelerator</code> by hand</b></summary>

Symptom: you added your own line to `/etc/synthetic.d/immich-accelerator` (e.g. a split-deployment upload path), rebooted, and Microservices is red. The native worker won't start because `/build` doesn't resolve.

Cause: that file also holds the required `/build` synthetic link (for Immich 2.7+ plugin paths). Before v1.5.7, setup treated the file *existing* as "build link configured" and skipped writing the entry, so a hand-edited file silently lost `/build`.

Fix: upgrade to v1.5.7+ and re-run setup; it now checks for the actual `build` entry and appends it without touching your other lines. Or add it yourself and reboot:

```bash
# /etc/synthetic.d/immich-accelerator (needs a build entry, tab-separated)
printf 'build\t%s\n' "${HOME#/}/.immich-accelerator/build-data" | sudo tee -a /etc/synthetic.d/immich-accelerator
```

</details>

<details>
<summary><b>ML jobs fail with "Machine learning request failed for all URLs"</b></summary>

Symptom: Immich's worker log shows ML requests failing with HTTP 500 on every URL, even though `immich-accelerator status` says the ML service is running.

Diagnose: run:

```bash
immich-accelerator ml-test
```

This exercises `/ping`, `/health`, CLIP visual, and OCR with a synthetic image. On any failure it tails the last 30 lines of `~/.immich-accelerator/logs/ml.log` and prints the three most common root-cause fixes. Paste the output in a GitHub issue if you're stuck.

Common causes:

- **Partial HuggingFace model cache**: `rm -rf ~/.cache/huggingface/hub/models--mlx-community--clip-vit-base-patch32` then `immich-accelerator start`
- **mlx / mlx-clip version mismatch**: `brew reinstall immich-accelerator`
- **Stale model files**: `rm -rf ~/.immich-accelerator/ml/models` then restart

</details>

<details>
<summary><b>Dashboard crashes with <code>ModuleNotFoundError: No module named 'uvicorn'</code></b></summary>

Fixed in v1.4.1. If you're on an older release, `brew upgrade immich-accelerator` and re-run. The formula wrapper now runs the CLI under the ML venv's Python, which has fastapi + uvicorn installed.

</details>

<details>
<summary><b><code>immich-accelerator setup</code> fails with <code>ENOENT: /build/corePlugin/manifest.json</code></b></summary>

Fixed in v1.4.1. The OCI image extractor used to skip small layers that contained the Immich 2.7+ `corePlugin` WASM files. Upgrade and re-run setup.

</details>

<details>
<summary><b><code>brew install</code> fails with "Refusing to load formula ... from untrusted tap"</b></summary>

Homebrew 5.1.15 (June 2026) requires third-party taps to be explicitly trusted before it will load their formulas. The fix is one command:

```bash
brew trust epheterson/immich-accelerator
```

Using the fully-qualified name (`brew install epheterson/immich-accelerator/immich-accelerator`, as in the quick start) bypasses the check for that one command (Homebrew treats naming the tap explicitly as consent), but `brew upgrade` still skips the tap until it's trusted.

</details>

<a id="brew-upgrade-never-finds-a-new-version"></a>
<details>
<summary><b><code>brew upgrade</code> never finds a new version</b></summary>

Symptom: `brew upgrade immich-accelerator` reports nothing to do (and `brew outdated` shows nothing), but GitHub has a newer release. `brew info immich-accelerator` shows the real error: `Refusing to load formula ... from untrusted tap`.

Cause: the same trust requirement as above, but for taps added *before* Homebrew 5.1.15 there's no error: Homebrew *silently skips* untrusted formulas during `outdated`/`upgrade`, so your install goes stale with no warning.

Fix:

```bash
brew trust epheterson/immich-accelerator
brew update && brew upgrade immich-accelerator
immich-accelerator stop && immich-accelerator start
```

</details>

Still stuck? Open an issue with the output of `immich-accelerator status` and `immich-accelerator ml-test`.
