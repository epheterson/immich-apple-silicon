"""Database access layer for Immich thumbnail worker.

Queries the Immich Postgres database to find assets needing thumbnails,
and writes back preview/thumbnail paths + thumbhash when done.
"""

import psycopg2
import psycopg2.extras


class ThumbnailDB:
    """Talks to the Immich Postgres database for thumbnail work."""

    # Container paths used by Immich inside Docker
    CONTAINER_UPLOAD = "/usr/src/app/upload/"
    CONTAINER_PHOTOS = "/mnt/photos/"

    def __init__(self, host: str, port: int, dbname: str, user: str, password: str,
                 upload_dir: str, photos_dir: str):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.upload_dir = upload_dir.rstrip("/") + "/"
        self.photos_dir = photos_dir.rstrip("/") + "/"

    def _connect(self):
        """Return a new psycopg2 connection."""
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
        )

    def translate_path(self, container_path: str) -> str:
        """Translate a container path to a host path.

        /usr/src/app/upload/... → upload_dir/...
        /mnt/photos/...        → photos_dir/...
        """
        if container_path.startswith(self.CONTAINER_UPLOAD):
            return self.upload_dir + container_path[len(self.CONTAINER_UPLOAD):]
        if container_path.startswith(self.CONTAINER_PHOTOS):
            return self.photos_dir + container_path[len(self.CONTAINER_PHOTOS):]
        return container_path

    def container_path(self, host_path: str) -> str:
        """Translate a host path back to a container path (reverse of translate_path)."""
        if host_path.startswith(self.upload_dir):
            return self.CONTAINER_UPLOAD + host_path[len(self.upload_dir):]
        if host_path.startswith(self.photos_dir):
            return self.CONTAINER_PHOTOS + host_path[len(self.photos_dir):]
        return host_path

    def get_pending_assets(self, limit: int = 20, asset_type: str = "IMAGE") -> list[dict]:
        """Return assets that need thumbnails (thumbhash IS NULL).

        Returns list of dicts with keys: id, originalPath, ownerId.
        """
        sql = """
            SELECT a.id, a."originalPath", a."ownerId"::text
            FROM asset a
            WHERE a.thumbhash IS NULL
              AND a.type = %s
              AND a."deletedAt" IS NULL
            ORDER BY a."createdAt" DESC
            LIMIT %s
        """
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (asset_type, limit))
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_complete(self, asset_id: str, owner_id: str,
                      preview_container_path: str, thumb_container_path: str,
                      thumbhash_bytes: bytes) -> None:
        """Mark an asset as having thumbnails generated.

        UPSERTs preview + thumbnail into asset_file, updates asset.thumbhash.
        """
        upsert_sql = """
            INSERT INTO asset_file ("assetId", type, path, "updateId", "isEdited", "isProgressive", "isTransparent")
            VALUES (%s, %s, %s, immich_uuid_v7(), false, false, false)
            ON CONFLICT ("assetId", type, "isEdited") DO UPDATE SET
                path = EXCLUDED.path, "updateId" = immich_uuid_v7(), "updatedAt" = now()
        """
        thumbhash_sql = """
            UPDATE asset SET thumbhash = %s, "updateId" = immich_uuid_v7() WHERE id = %s
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # UPSERT preview
                cur.execute(upsert_sql, (asset_id, "preview", preview_container_path))
                # UPSERT thumbnail
                cur.execute(upsert_sql, (asset_id, "thumbnail", thumb_container_path))
                # Update thumbhash
                cur.execute(thumbhash_sql, (psycopg2.Binary(thumbhash_bytes), asset_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Return thumbnail generation stats.

        Returns dict with keys: total, done, pending_images, pending_videos.
        """
        sql = """
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE thumbhash IS NOT NULL) AS done,
                count(*) FILTER (WHERE thumbhash IS NULL AND type = 'IMAGE' AND "deletedAt" IS NULL) AS pending_images,
                count(*) FILTER (WHERE thumbhash IS NULL AND type = 'VIDEO' AND "deletedAt" IS NULL) AS pending_videos
            FROM asset
        """
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return dict(row)
        finally:
            conn.close()
