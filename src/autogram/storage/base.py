"""The storage interface every backend implements.

Storage fills two roles (DESIGN.md §6.1), and a backend may serve one or both:

- **authoring** — where the user arranges folders and writes captions, from
  desktop or phone. Needs to support listing, reading, writing and moving.
- **serving** — where Instagram fetches media over HTTPS. Needs
  ``fetchable_url``.

Google Drive can author but cannot serve: its share links return an HTML viewer
page rather than raw bytes. GCS can serve but is unpleasant to browse on a
phone. Hence the recommended pairing of the two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class StorageEntry:
    """One file or folder in storage.

    ``path`` is always relative to the backend's configured root, using forward
    slashes, so callers never deal with backend-native identifiers. Drive's file
    IDs, for instance, stay inside the Drive backend.
    """

    path: str
    is_dir: bool
    size: int | None = None


class Storage(ABC):
    """Base class for all storage backends.

    Implementations must be safe to construct from a single connection string
    (DESIGN.md §6.2.2) and must not require any other configuration.
    """

    #: Whether this backend can hand Instagram a fetchable URL. Serving
    #: backends set this True and implement ``fetchable_url``.
    can_serve: bool = False

    # --- authoring -----------------------------------------------------

    @abstractmethod
    def list(self, path: str) -> list[StorageEntry]:
        """List direct children of ``path``. Returns [] if it does not exist."""

    @abstractmethod
    def read(self, path: str) -> bytes:
        """Read a file whole. Raises FileNotFoundError if absent."""

    @abstractmethod
    def write(self, path: str, data: bytes) -> None:
        """Write a file, creating parent folders and overwriting if present."""

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Move a file or folder. Used to retire published posts."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete a file or folder. Used to clean up staged media."""

    # --- serving -------------------------------------------------------

    def fetchable_url(self, path: str, expires_in: timedelta) -> str:
        """Return an HTTPS URL Instagram's fetcher can retrieve.

        Only serving backends implement this. The URL must return raw bytes —
        no viewer page, no redirect to one — and stay valid for at least
        ``expires_in``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot serve media to Instagram. "
            "Configure a serving backend (GCS or S3) alongside it."
        )

