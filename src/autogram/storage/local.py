"""Filesystem storage.

Not for production use — Instagram cannot fetch from a local disk — but it
implements the full interface, which makes it the backend to develop and test
against. Every contract test runs against this first; Drive and GCS then have
to satisfy the same suite.

    local:///home/me/autogram-content
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from autogram.storage.base import Storage, StorageEntry


class LocalStorage(Storage):
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_dsn(cls, dsn: str) -> "LocalStorage":
        _, _, path = dsn.partition("://")
        if not path:
            raise ValueError("local:// connection string needs a path")
        return cls(path)

    def _resolve(self, path: str) -> Path:
        """Map a storage path to a real one, refusing to escape the root."""
        candidate = (self.root / path.strip("/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Path escapes storage root: {path!r}")
        return candidate

    def list(self, path: str) -> list[StorageEntry]:
        target = self._resolve(path)
        if not target.is_dir():
            return []
        entries = [
            StorageEntry(
                path=f"{path.strip('/')}/{child.name}".lstrip("/"),
                is_dir=child.is_dir(),
                size=None if child.is_dir() else child.stat().st_size,
            )
            for child in target.iterdir()
        ]
        return sorted(entries, key=lambda e: e.path)

    def read(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()

    def write(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def move(self, src: str, dst: str) -> None:
        source = self._resolve(src)
        if not source.exists():
            raise FileNotFoundError(src)
        destination = self._resolve(dst)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def fetchable_url(self, path: str, expires_in: timedelta) -> str:
        raise NotImplementedError(
            "LocalStorage cannot serve media to Instagram — it is for "
            "development and testing only. Configure gs:// or s3:// to publish."
        )
