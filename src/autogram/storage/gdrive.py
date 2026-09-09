"""Google Drive — the authoring backend.

    gdrive://<client_id>:<client_secret>:<refresh_token>@<folder_id>

Drive is where the user actually works: arranging photos into folders and
writing captions, from a phone or a desktop. It cannot serve media to Instagram
— its share links return an HTML viewer page rather than raw bytes — so it is
always paired with an object store.

Two things shape this implementation:

**Drive has no paths, only file IDs.** A path like ``queue/2026-09-20/01.jpg``
has to be walked segment by segment, one lookup per level. Results are cached
per instance, since a single run touches the same folders repeatedly.

**The full ``drive`` scope is required.** The narrower ``drive.file`` covers
only files the app itself created or the user picked through Google's Picker
UI, so folders made by hand in the Drive mobile app would be invisible — which
is precisely the workflow this exists to support.
"""

from __future__ import annotations

import io
import logging
from datetime import timedelta

from autogram.storage.base import Storage, StorageEntry
from autogram.storage.dsn import parse

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(Exception):
    """Drive could not satisfy a request in a way the user should know about."""


class GDriveStorage(Storage):
    def __init__(
        self, client_id: str, client_secret: str, refresh_token: str, root_folder_id: str
    ):
        self.root_folder_id = root_folder_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._service = None
        # Path → file id. A run resolves "queue" and its children many times.
        self._ids: dict[str, str] = {"": root_folder_id}

    @classmethod
    def from_dsn(cls, dsn: str) -> "GDriveStorage":
        parsed = parse(dsn, expected_credentials=3)
        client_id, client_secret, refresh_token = parsed.credentials
        return cls(client_id, client_secret, refresh_token, parsed.location)

    @property
    def service(self):
        """The Drive client, built lazily so construction stays cheap."""
        if self._service is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            credentials = Credentials(
                token=None,
                refresh_token=self._refresh_token,
                token_uri=TOKEN_URI,
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=SCOPES,
            )
            credentials.refresh(Request())
            self._service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        return self._service

    # --- path resolution -----------------------------------------------

    def _child_id(self, parent_id: str, name: str) -> str | None:
        """Find one child by name, or None. Raises if the name is ambiguous."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        response = (
            self.service.files()
            .list(
                q=f"'{parent_id}' in parents and name = '{escaped}' and trashed = false",
                fields="files(id, name, mimeType)",
                pageSize=2,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        if not files:
            return None
        if len(files) > 1:
            # Drive permits duplicate names in one folder; guessing which is
            # meant would post the wrong content, so refuse instead.
            raise DriveError(
                f"Two or more items named {name!r} share a folder in Drive. "
                "Rename one — Autogram cannot tell which you mean."
            )
        return files[0]["id"]

    def _resolve(self, path: str) -> str | None:
        """Resolve a storage path to a Drive file id, or None if absent."""
        path = path.strip("/")
        if path in self._ids:
            return self._ids[path]

        parent_path, _, name = path.rpartition("/")
        parent_id = self._resolve(parent_path)
        if parent_id is None:
            return None

        file_id = self._child_id(parent_id, name)
        if file_id is not None:
            self._ids[path] = file_id
        return file_id

    def _resolve_or_raise(self, path: str) -> str:
        file_id = self._resolve(path)
        if file_id is None:
            raise FileNotFoundError(path)
        return file_id

    def _ensure_folder(self, path: str) -> str:
        """Resolve a folder path, creating any missing levels."""
        path = path.strip("/")
        if not path:
            return self.root_folder_id
        if path in self._ids:
            return self._ids[path]

        parent_path, _, name = path.rpartition("/")
        parent_id = self._ensure_folder(parent_path)

        existing = self._child_id(parent_id, name)
        if existing is None:
            created = (
                self.service.files()
                .create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            existing = created["id"]

        self._ids[path] = existing
        return existing

    def _forget(self, path: str) -> None:
        """Drop cached ids for a path and anything beneath it."""
        path = path.strip("/")
        for cached in [k for k in self._ids if k == path or k.startswith(f"{path}/")]:
            if cached:
                del self._ids[cached]

    # --- interface -----------------------------------------------------

    def list(self, path: str) -> list[StorageEntry]:
        folder_id = self._resolve(path)
        if folder_id is None:
            return []

        entries: list[StorageEntry] = []
        page_token = None
        base = path.strip("/")

        while True:
            response = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in response.get("files", []):
                child_path = f"{base}/{item['name']}" if base else item["name"]
                self._ids[child_path] = item["id"]
                entries.append(
                    StorageEntry(
                        path=child_path,
                        is_dir=item["mimeType"] == FOLDER_MIME,
                        size=int(item["size"]) if item.get("size") else None,
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return sorted(entries, key=lambda e: e.path)

    def read(self, path: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        file_id = self._resolve_or_raise(path)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buffer, self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def write(self, path: str, data: bytes) -> None:
        from googleapiclient.http import MediaIoBaseUpload

        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        parent_id = self._ensure_folder(parent_path)

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/octet-stream")
        existing = self._child_id(parent_id, name)

        if existing:
            # Update in place — creating a second file with the same name is
            # legal in Drive and would leave two copies behind.
            self.service.files().update(
                fileId=existing, media_body=media, supportsAllDrives=True
            ).execute()
            self._ids[path] = existing
        else:
            created = (
                self.service.files()
                .create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            self._ids[path] = created["id"]

    def move(self, src: str, dst: str) -> None:
        """Move a file or folder. Moving a folder takes its contents along."""
        file_id = self._resolve_or_raise(src)

        dst = dst.strip("/")
        new_parent_path, _, new_name = dst.rpartition("/")
        new_parent_id = self._ensure_folder(new_parent_path)

        current = (
            self.service.files()
            .get(fileId=file_id, fields="parents", supportsAllDrives=True)
            .execute()
        )
        old_parents = ",".join(current.get("parents", []))

        self.service.files().update(
            fileId=file_id,
            body={"name": new_name},
            addParents=new_parent_id,
            removeParents=old_parents,
            supportsAllDrives=True,
        ).execute()

        self._forget(src)
        self._ids[dst] = file_id

    def delete(self, path: str) -> None:
        file_id = self._resolve(path)
        if file_id is None:
            return
        # Trash rather than destroy: this deletes a user's content, and Drive's
        # trash gives them 30 days to undo a mistake of ours.
        self.service.files().update(
            fileId=file_id, body={"trashed": True}, supportsAllDrives=True
        ).execute()
        self._forget(path)

    def fetchable_url(self, path: str, expires_in: timedelta) -> str:
        raise NotImplementedError(
            "Google Drive cannot serve media to Instagram: its links return a "
            "viewer page rather than the file itself. Add a gs:// or s3:// "
            "connection string as the second line of AUTOGRAM_STORAGE."
        )
