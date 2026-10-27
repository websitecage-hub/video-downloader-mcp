from __future__ import annotations

import mimetypes
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .security import safe_filename


class StorageFullError(RuntimeError):
    pass


@dataclass
class StoredFile:
    token: str
    path: Path
    filename: str
    content_type: str
    size: int
    created_at: float
    expires_at: float
    title: str
    source_url: str
    platform: str
    duration: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def size_mb(self) -> float:
        return round(self.size / (1024 * 1024), 2)


class FileStore:
    """Tracks temporary media files and expires them automatically."""

    def __init__(self, root: Path, ttl_seconds: int, max_total_bytes: int):
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.max_total_bytes = max_total_bytes
        self._files: dict[str, StoredFile] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        source_path: Path,
        *,
        title: str,
        source_url: str,
        platform: str,
        duration: float | None = None,
        filename: str | None = None,
        extra: dict | None = None,
    ) -> StoredFile:
        size = source_path.stat().st_size
        if size <= 0:
            raise StorageFullError("The downloaded file is empty.")
        if size > self.max_total_bytes:
            raise StorageFullError("The downloaded file is larger than the server's storage limit.")

        token = secrets.token_urlsafe(24)
        ext = source_path.suffix.lstrip(".") or "mp4"
        clean_name = filename or safe_filename(title, ext)
        target_dir = self.root / token
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / clean_name

        with self._lock:
            if self._total_bytes() + size > self.max_total_bytes:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise StorageFullError(
                    "The server's temporary storage is full. Try again in a few minutes."
                )
            shutil.move(str(source_path), str(target))
            now = time.time()
            item = StoredFile(
                token=token,
                path=target,
                filename=clean_name,
                content_type=mimetypes.guess_type(clean_name)[0] or "application/octet-stream",
                size=size,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                title=title,
                source_url=source_url,
                platform=platform,
                duration=duration,
                extra=dict(extra or {}),
            )
            self._files[token] = item
            return item

    def get(self, token: str) -> StoredFile | None:
        with self._lock:
            item = self._files.get(token)
            if item is None:
                return None
            if item.expires_at <= time.time():
                self._remove_locked(token)
                return None
            return item

    def remove(self, token: str) -> bool:
        """Immediately remove a stored file and its directory."""
        with self._lock:
            if token not in self._files:
                return False
            self._remove_locked(token)
            return True

    def cleanup(self) -> int:
        removed = 0
        now = time.time()
        with self._lock:
            for token, item in list(self._files.items()):
                if item.expires_at <= now:
                    self._remove_locked(token)
                    removed += 1
            # Remove orphan directories left behind by crashed jobs or previous
            # process lifetimes. Active job directories are recent, so only
            # delete unknown directories older than the TTL plus a grace period.
            known = set(self._files)
            cutoff = now - self.ttl_seconds - 300
            for child in self.root.iterdir():
                if not child.is_dir() or child.name in known:
                    continue
                try:
                    if child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    pass
        return removed

    def stats(self) -> dict:
        with self._lock:
            return {
                "files": len(self._files),
                "total_mb": round(self._total_bytes() / (1024 * 1024), 2),
                "max_mb": round(self.max_total_bytes / (1024 * 1024), 2),
                "ttl_minutes": round(self.ttl_seconds / 60),
            }

    def _total_bytes(self) -> int:
        return sum(item.size for item in self._files.values())

    def _remove_locked(self, token: str) -> None:
        item = self._files.pop(token, None)
        if item is not None:
            shutil.rmtree(item.path.parent, ignore_errors=True)
