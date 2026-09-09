"""Storage backends.

``base.Storage`` defines the interface; each backend is constructible from a
single connection string. See DESIGN.md §6.
"""

from autogram.storage.base import Storage, StorageEntry
from autogram.storage.factory import from_dsn

__all__ = ["Storage", "StorageEntry", "from_dsn"]
