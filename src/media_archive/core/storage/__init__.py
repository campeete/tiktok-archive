"""
Storage abstraction for transcripts, metadata JSON, and DB backups.

Two backends:
- LocalStorage: writes to disk under TRANSCRIPTS_DIR / DB_BACKUPS_DIR.
- R2Storage: S3-compatible Cloudflare R2 bucket.

Architecture: local is always written first (it's the operational store).
R2 is a durable mirror written after local succeeds. If R2 fails, we record
the failure but don't roll back the local write — the worker can retry the
sync later.
"""
from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from media_archive.core import config

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Minimal interface, deliberately small."""

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Write data at key. Idempotent (overwrite is fine)."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read data at key. Raises KeyError if missing."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete key. Returns True if it existed."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if key exists."""

    @abstractmethod
    def list(self, prefix: str = "") -> Iterable[str]:
        """Yield keys with the given prefix."""


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------

class LocalStorage(StorageBackend):
    """Writes to a base directory on disk.

    Keys are interpreted as relative paths. Directories are auto-created.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or config.DATA_DIR).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Defense against path traversal
        p = (self.base_dir / key).resolve()
        if not str(p).startswith(str(self.base_dir)):
            raise ValueError(f"Key escapes base dir: {key}")
        return p

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via tempfile + rename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> Iterable[str]:
        root = self._path(prefix) if prefix else self.base_dir
        if not root.exists():
            return
        for p in root.rglob("*"):
            if p.is_file():
                yield str(p.relative_to(self.base_dir))


# ---------------------------------------------------------------------------
# R2 (S3-compatible)
# ---------------------------------------------------------------------------

class R2Storage(StorageBackend):
    """Cloudflare R2 backend. Lazy boto3 import so we don't pull it in unless used."""

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        endpoint_url: str | None = None,
    ) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "R2 backend requires boto3. Install with: "
                "pip install -e '.[r2]'"
            ) from e

        self.bucket = bucket
        endpoint = endpoint_url or config.r2_endpoint_url()
        if not endpoint:
            raise ValueError("R2 endpoint URL is required")

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise KeyError(key)

    def delete(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        self.client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def list(self, prefix: str = "") -> Iterable[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


# ---------------------------------------------------------------------------
# Mirrored backend: local primary + cloud durable mirror
# ---------------------------------------------------------------------------

class MirroredStorage(StorageBackend):
    """Writes to primary, then mirrors to secondary asynchronously.

    Reads come from primary first, fall back to secondary on miss.
    Used when R2 is enabled — local is primary, R2 is mirror.
    """

    def __init__(self, primary: StorageBackend, mirror: StorageBackend) -> None:
        self.primary = primary
        self.mirror = mirror

    def put(self, key: str, data: bytes) -> None:
        self.primary.put(key, data)
        try:
            self.mirror.put(key, data)
        except Exception as e:
            logger.warning("Mirror put failed for %s: %s", key, e)

    def get(self, key: str) -> bytes:
        try:
            return self.primary.get(key)
        except KeyError:
            return self.mirror.get(key)

    def delete(self, key: str) -> bool:
        primary_existed = self.primary.delete(key)
        try:
            self.mirror.delete(key)
        except Exception as e:
            logger.warning("Mirror delete failed for %s: %s", key, e)
        return primary_existed

    def exists(self, key: str) -> bool:
        return self.primary.exists(key) or self.mirror.exists(key)

    def list(self, prefix: str = "") -> Iterable[str]:
        seen = set()
        for k in self.primary.list(prefix):
            if k not in seen:
                seen.add(k)
                yield k
        for k in self.mirror.list(prefix):
            if k not in seen:
                seen.add(k)
                yield k


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_storage() -> StorageBackend:
    """Construct the configured storage backend.

    - local: LocalStorage rooted at DATA_DIR
    - r2: MirroredStorage(local + r2)

    R2-only mode (no local mirror) is intentionally not supported. Reading
    transcripts for the web UI must be fast and local.
    """
    local = LocalStorage(config.DATA_DIR)
    if config.STORAGE_BACKEND == "r2":
        if not config.r2_configured():
            logger.warning(
                "STORAGE_BACKEND=r2 but R2 credentials are missing. "
                "Falling back to local-only."
            )
            return local
        try:
            r2 = R2Storage(
                account_id=config.R2_ACCOUNT_ID,
                access_key_id=config.R2_ACCESS_KEY_ID,
                secret_access_key=config.R2_SECRET_ACCESS_KEY,
                bucket=config.R2_BUCKET_NAME,
                endpoint_url=config.r2_endpoint_url(),
            )
            return MirroredStorage(local, r2)
        except Exception as e:
            logger.warning("Failed to init R2: %s. Falling back to local-only.", e)
            return local
    return local


def test_r2_connection() -> tuple[bool, str]:
    """Smoke-test the configured R2 connection. Returns (ok, message)."""
    if config.STORAGE_BACKEND != "r2":
        return False, "STORAGE_BACKEND is not 'r2'"
    if not config.r2_configured():
        return False, "R2 credentials are not fully configured in .env"
    try:
        r2 = R2Storage(
            account_id=config.R2_ACCOUNT_ID,
            access_key_id=config.R2_ACCESS_KEY_ID,
            secret_access_key=config.R2_SECRET_ACCESS_KEY,
            bucket=config.R2_BUCKET_NAME,
        )
        test_key = ".tiktok-archive-test"
        payload = b"connection-test"
        r2.put(test_key, payload)
        got = r2.get(test_key)
        r2.delete(test_key)
        if got != payload:
            return False, "Round-trip data mismatch"
        return True, f"OK — bucket '{config.R2_BUCKET_NAME}' reachable"
    except Exception as e:
        return False, f"R2 error: {type(e).__name__}: {e}"
