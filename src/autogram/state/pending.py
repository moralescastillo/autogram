"""The in-flight marker that closes the crash window.

The ledger stops a *completed* publish from repeating. It cannot stop a
duplicate when a run dies between Instagram accepting the post and the ledger
being written — which is plausible when a runner hits its timeout partway
through a video.

So the container id is written into the post's own folder *before* publishing.
If a later run finds one, it asks Instagram what became of it:

===============  ==================================  =========================
``status_code``  Meaning                             Action
===============  ==================================  =========================
``PUBLISHED``    It went out; bookkeeping was lost    Record it, retire the post
``FINISHED``     Ready but never published            Publish it
``ERROR``        Instagram rejected it                Fail the post, start over
``EXPIRED``      Not published within 24 hours        Start over
===============  ==================================  =========================

The file lives in the post folder rather than ``state/`` so it travels with the
post when it moves, and so a user glancing at their Drive can see which post
was mid-flight.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PENDING_FILE = "publishing.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Pending:
    """A publish that was started but not confirmed."""

    container_id: str
    post_type: str
    created_at: datetime
    staged: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "container_id": self.container_id,
                "post_type": self.post_type,
                "created_at": self.created_at.isoformat(),
                "staged": self.staged,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Pending":
        payload = json.loads(raw)
        created = datetime.fromisoformat(payload["created_at"])
        return cls(
            container_id=payload["container_id"],
            post_type=payload.get("post_type", ""),
            created_at=created if created.tzinfo else created.replace(tzinfo=timezone.utc),
            staged=list(payload.get("staged", [])),
        )


class PendingStore:
    def __init__(self, storage):
        self.storage = storage

    def _path(self, folder: str) -> str:
        return f"{folder.rstrip('/')}/{PENDING_FILE}"

    def read(self, folder: str) -> Pending | None:
        """Return the in-flight marker for a post, if there is one."""
        try:
            return Pending.from_json(self.storage.read(self._path(folder)).decode("utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # An unreadable marker cannot prove anything was published, and
            # guessing either way risks a duplicate or a lost post.
            log.warning("Unreadable %s in %s (%s); ignoring it.", PENDING_FILE, folder, exc)
            return None

    def write(
        self,
        folder: str,
        container_id: str,
        *,
        post_type: str = "",
        staged: list[str] | None = None,
        now: datetime | None = None,
    ) -> Pending:
        """Record a container about to be published.

        For a carousel this is the *parent* container — the one that would be
        handed to ``publish()``.
        """
        pending = Pending(
            container_id=container_id,
            post_type=post_type,
            created_at=now or _now(),
            staged=list(staged or []),
        )
        self.storage.write(self._path(folder), pending.to_json().encode("utf-8"))
        return pending

    def clear(self, folder: str) -> None:
        """Remove the marker once the outcome is recorded."""
        self.storage.delete(self._path(folder))
