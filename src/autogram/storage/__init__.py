"""Storage backends.

``base.Storage`` defines the interface; each backend is constructible from a
single connection string. See DESIGN.md §6.
"""

from autogram.storage.base import Storage, StorageEntry

__all__ = ["Storage", "StorageEntry"]
