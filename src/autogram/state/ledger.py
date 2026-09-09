"""The publish ledger: what went out, and when.

Two jobs. It stops a post being published twice, and it tells the scheduler
when the last post went out — which is what makes a late run publish rather
than lose its slot (DESIGN.md §7.2).

**Corruption must never look like emptiness.** If a half-written
``published.json`` were read as "nothing has been published", every post in
``published/`` would be republished. So a file that exists but cannot be parsed
raises, and the run fails loudly with the post untouched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

LEDGER_PATH = "state/published.json"


class LedgerCorrupt(Exception):
    """The ledger exists but cannot be read. Never treated as empty."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Entry:
    name: str
    published_at: datetime
    media_id: str | None = None
    post_type: str | None = None
    immediate: bool = False
    recovered: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "published_at": self.published_at.isoformat(),
            "media_id": self.media_id,
            "post_type": self.post_type,
            "immediate": self.immediate,
            "recovered": self.recovered,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Entry":
        return cls(
            name=payload["name"],
            published_at=_parse_time(payload["published_at"]),
            media_id=payload.get("media_id"),
            post_type=payload.get("post_type"),
            immediate=bool(payload.get("immediate", False)),
            recovered=bool(payload.get("recovered", False)),
        )


class Ledger:
    def __init__(self, storage, path: str = LEDGER_PATH):
        self.storage = storage
        self.path = path
        self._entries: list[Entry] | None = None

    def entries(self) -> list[Entry]:
        if self._entries is None:
            self._entries = self._read()
        return self._entries

    def _read(self) -> list[Entry]:
        try:
            raw = self.storage.read(self.path).decode("utf-8")
        except FileNotFoundError:
            return []

        try:
            payload = json.loads(raw)
            return [Entry.from_dict(item) for item in payload.get("published", [])]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # Reading this as empty would republish everything ever posted.
            raise LedgerCorrupt(
                f"{self.path} exists but could not be read ({exc}). "
                f"Nothing will be published until it is fixed or removed — "
                f"treating it as empty would repost your entire history."
            ) from exc

    def is_published(self, name: str) -> bool:
        return any(entry.name == name for entry in self.entries())

    def find(self, name: str) -> Entry | None:
        return next((e for e in self.entries() if e.name == name), None)

    def last_published(self, *, scheduled_only: bool = False) -> datetime | None:
        """When the most recent post went out.

        ``scheduled_only`` excludes posts published through ``now/``. Whether an
        immediate post should consume the day's scheduled slot is a policy
        question; the ledger only makes it answerable.
        """
        entries = self.entries()
        if scheduled_only:
            entries = [e for e in entries if not e.immediate]
        return max((e.published_at for e in entries), default=None)

    def record(
        self,
        name: str,
        *,
        media_id: str | None = None,
        post_type: str | None = None,
        immediate: bool = False,
        recovered: bool = False,
        now: datetime | None = None,
    ) -> Entry:
        """Record a published post and persist immediately."""
        entry = Entry(
            name=name,
            published_at=now or _now(),
            media_id=media_id,
            post_type=post_type,
            immediate=immediate,
            recovered=recovered,
        )

        entries = self.entries()
        entries.append(entry)
        self._write(entries)
        return entry

    def _write(self, entries: list[Entry]) -> None:
        payload = {
            "version": 1,
            "published": [entry.to_dict() for entry in entries],
        }
        self.storage.write(self.path, json.dumps(payload, indent=2).encode("utf-8"))
        self._entries = entries
