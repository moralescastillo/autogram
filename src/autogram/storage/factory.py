"""Build a storage backend from a connection string."""

from __future__ import annotations

from autogram.config import ConfigError
from autogram.storage.base import Storage


def from_dsn(dsn: str) -> Storage:
    """Construct the backend a connection string names."""
    scheme = dsn.split("://", 1)[0].strip().lower()

    if scheme == "gdrive":
        from autogram.storage.gdrive import GDriveStorage

        return GDriveStorage.from_dsn(dsn)

    if scheme in ("gs", "s3"):
        from autogram.storage.objectstore import ObjectStore

        return ObjectStore.from_dsn(dsn)

    if scheme == "local":
        from autogram.storage.local import LocalStorage

        return LocalStorage.from_dsn(dsn)

    if scheme == "dropbox":
        raise ConfigError(
            "Dropbox support is not implemented yet. Use gdrive:// for "
            "authoring and gs:// or s3:// for serving."
        )

    raise ConfigError(f"Unknown storage scheme {scheme!r}.")
