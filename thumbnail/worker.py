"""Thumbnail worker — polls Immich DB and generates preview + thumbnail + thumbhash."""
from __future__ import annotations

import gc
import logging
import os
import signal
import time
from types import FrameType

from thumbnail.db import ThumbnailDB
from thumbnail.resize import generate_all

log = logging.getLogger(__name__)

# Back off exponentially when consecutive errors exceed this threshold.
# Handles Postgres restarts (Immich updates, migrations) and schema changes
# without hammering the database or requiring manual stop/start.
_MAX_CONSECUTIVE_ERRORS = 5
_MAX_BACKOFF_SECONDS = 300  # 5 minutes


class ThumbnailWorker:
    """Processes Immich assets that are missing thumbnails.

    Polls the database for pending assets, generates preview (1440px JPEG)
    and thumbnail (250px WebP) via Metal-accelerated resize, computes
    thumbhash, and writes results back to the DB.

    Designed to survive Immich restarts and updates gracefully:
    - Transient DB errors (Postgres restart): retries with backoff, auto-recovers
    - Schema changes (Immich migration): backs off, logs clear error, waits
    - Watchtower / auto-updates: no manual intervention needed
    """

    PREVIEW_MAX_DIM = 1440
    THUMBNAIL_MAX_DIM = 250
    JPEG_QUALITY = 80

    def __init__(self, db: ThumbnailDB, upload_dir: str,
                 batch_size: int = 20, poll_interval: int = 5):
        self.db = db
        self.upload_dir = upload_dir.rstrip("/")
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.processed = 0
        self.errors = 0
        self._running = True
        self._consecutive_errors = 0
        self._consecutive_batch_failures = 0

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        log.info("Received %s — finishing current asset then stopping", name)
        self._running = False

    def _backoff_seconds(self) -> int:
        """Exponential backoff based on consecutive error count."""
        if self._consecutive_errors <= _MAX_CONSECUTIVE_ERRORS:
            return self.poll_interval
        power = min(self._consecutive_errors - _MAX_CONSECUTIVE_ERRORS, 8)
        return min(self.poll_interval * (2 ** power), _MAX_BACKOFF_SECONDS)

    def _available_memory_mb(self) -> int:
        """Check available memory (free + inactive pages) via vm_stat."""
        try:
            import subprocess, re
            vm = subprocess.check_output(["vm_stat"], timeout=5).decode()
            # Parse page size from vm_stat header (16384 on Apple Silicon, 4096 on Intel)
            ps_match = re.search(r"page size of (\d+) bytes", vm)
            page_size = int(ps_match.group(1)) if ps_match else 16384
            free_match = re.search(r"Pages free:\s+(\d+)", vm)
            inactive_match = re.search(r"Pages inactive:\s+(\d+)", vm)
            if not free_match or not inactive_match:
                return 9999
            free = int(free_match.group(1))
            inactive = int(inactive_match.group(1))
            return (free + inactive) * page_size // (1024 * 1024)
        except Exception:
            return 9999  # assume OK if we can't check

    def _output_dir(self, owner_id: str, asset_id: str) -> str:
        """Build the output directory for an asset's thumbnails."""
        stripped = asset_id.replace("-", "")
        return os.path.join(
            self.upload_dir, "thumbs", owner_id,
            stripped[0:2], stripped[2:4],
        )

    def process_asset(self, asset_id: str, original_path: str, owner_id: str) -> bool:
        """Generate preview + thumbnail + thumbhash for a single asset.

        Returns True on success, False on failure.
        """
        host_path = self.db.translate_path(original_path)

        if not os.path.isfile(host_path):
            log.error("Source file missing: %s (container: %s)", host_path, original_path)
            self.errors += 1
            return False

        out_dir = self._output_dir(owner_id, asset_id)
        preview_path = os.path.join(out_dir, f"{asset_id}_preview.jpeg")
        thumb_path = os.path.join(out_dir, f"{asset_id}_thumbnail.webp")

        try:
            os.makedirs(out_dir, exist_ok=True)

            # Single-pass: load image once, GPU resize both, compute thumbhash
            pw, ph, tw, th, thumbhash = generate_all(
                host_path, preview_path, thumb_path,
                preview_max=self.PREVIEW_MAX_DIM,
                thumb_max=self.THUMBNAIL_MAX_DIM,
                quality=self.JPEG_QUALITY,
            )

            # Container paths for DB
            preview_container = self.db.container_path(preview_path)
            thumb_container = self.db.container_path(thumb_path)

            # Write to DB
            self.db.mark_complete(
                asset_id=asset_id,
                preview_container_path=preview_container,
                thumb_container_path=thumb_container,
                thumbhash_bytes=thumbhash,
            )

            self.processed += 1
            self._consecutive_errors = 0
            log.info("OK %s (%dx%d preview, %dx%d thumb, %d-byte hash)",
                     asset_id, pw, ph, tw, th, len(thumbhash))
            return True

        except Exception:
            self.errors += 1
            self._consecutive_errors += 1
            log.exception("FAILED %s", asset_id)
            # Clean up partial output
            for path in (preview_path, thumb_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            return False

    def run(self) -> None:
        """Main loop — poll for pending assets, process, repeat.

        Handles Immich restarts and updates gracefully:
        - On transient DB errors: retries with exponential backoff
        - On sustained errors (schema change): backs off to 5min intervals
        - On recovery: immediately resumes normal processing
        """
        log.info("Worker started (batch=%d, poll=%ds, upload=%s)",
                 self.batch_size, self.poll_interval, self.upload_dir)

        # Prevent macOS Spotlight from indexing generated thumbnails.
        for subdir in ("thumbs", "encoded-video"):
            marker = os.path.join(self.upload_dir, subdir, ".metadata_never_index")
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                if not os.path.exists(marker):
                    with open(marker, "w"):
                        pass
            except OSError:
                pass

        batch_start = time.monotonic()

        while self._running:
            # Back off when system memory is low (< 500MB available)
            avail = self._available_memory_mb()
            if avail < 500:
                log.warning("Low memory (%dMB available) — pausing for %ds",
                            avail, self.poll_interval * 2)
                time.sleep(self.poll_interval * 2)
                continue

            # Fetch pending assets
            try:
                assets = self.db.get_pending_assets(limit=self.batch_size)
            except Exception:
                self._consecutive_errors += 1
                wait = self._backoff_seconds()
                if self._consecutive_errors <= _MAX_CONSECUTIVE_ERRORS:
                    log.warning("DB query failed — retrying in %ds (attempt %d)",
                                wait, self._consecutive_errors)
                else:
                    log.error("DB query failed %d times — backing off %ds. "
                              "Immich may be updating or schema may have changed. "
                              "Will auto-recover when DB is available.",
                              self._consecutive_errors, wait)
                time.sleep(wait)
                continue

            # Idle — no pending work
            if not assets:
                self._consecutive_errors = 0
                elapsed = time.monotonic() - batch_start
                if self.processed > 0:
                    rate = self.processed / max(elapsed, 0.001)
                    log.info("Idle — %d processed, %d errors, %.1f assets/sec",
                             self.processed, self.errors, rate)
                else:
                    log.info("Idle — no pending assets")
                time.sleep(self.poll_interval)
                continue

            # Process batch
            batch_success = 0
            for asset in assets:
                if not self._running:
                    break
                if self.process_asset(
                    asset_id=asset["id"],
                    original_path=asset["originalPath"],
                    owner_id=asset["ownerId"],
                ):
                    batch_success += 1

            # Free CIImage/CGImage/PIL objects from this batch before the next one.
            # Without this, Python's GC holds onto them until the next collection
            # cycle, wasting RAM during large imports.
            gc.collect()

            # If entire batch failed, back off (likely DB/schema issue, not bad images).
            # Use a separate counter so per-asset errors don't jump straight to max backoff.
            if batch_success == 0 and len(assets) > 0:
                self._consecutive_batch_failures += 1
                wait = min(self.poll_interval * (2 ** self._consecutive_batch_failures),
                           _MAX_BACKOFF_SECONDS)
                log.warning("Entire batch failed (%d assets, streak %d) — backing off %ds",
                            len(assets), self._consecutive_batch_failures, wait)
                time.sleep(wait)
                continue
            else:
                self._consecutive_batch_failures = 0

            # Log rate after each batch
            elapsed = time.monotonic() - batch_start
            rate = self.processed / max(elapsed, 0.001)
            log.info("Batch done — %d processed, %d errors, %.1f assets/sec",
                     self.processed, self.errors, rate)

        log.info("Worker stopped — %d processed, %d errors total",
                 self.processed, self.errors)
