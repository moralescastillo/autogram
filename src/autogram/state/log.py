"""The event log — machine-readable, for someone else's reporting tool.

One JSON object per line, one file per month, in the user's storage. This
codebase deliberately knows nothing about email: the previous system embedded
SMTP credentials in source and they leaked. Anything wanting to send a digest
reads these files.

**Events, not runs.** The design said one record per run, but the workflow runs
hourly — that is over 700 "nothing to do" lines a month, which no reporting
tool wants and which would bury the six lines that matter. Only things that
actually happened are recorded; the per-run trace lives in the Actions log,
where it is already free.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

LOG_DIR = "state/log"

PUBLISHED = "published"
FAILED = "failed"
RETRY_LATER = "retry_later"
RECOVERED = "recovered"
TOKEN_REFRESHED = "token_refreshed"
TOKEN_WARNING = "token_warning"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EventLog:
    def __init__(self, storage, directory: str = LOG_DIR):
        self.storage = storage
        self.directory = directory

    def path_for(self, when: datetime) -> str:
        return f"{self.directory}/{when:%Y-%m}.jsonl"

    def emit(self, event: str, *, now: datetime | None = None, **fields) -> dict:
        """Append one event. Never raises — logging must not break publishing."""
        now = now or _now()
        record = {"ts": now.isoformat(), "event": event, **fields}

        line = json.dumps(record, default=str)
        path = self.path_for(now)

        try:
            # Storage has no append, so read-modify-write. Runs are serialised
            # by the workflow's concurrency group, so there is no race here.
            try:
                existing = self.storage.read(path).decode("utf-8").rstrip("\n")
                body = f"{existing}\n{line}\n" if existing else f"{line}\n"
            except FileNotFoundError:
                body = f"{line}\n"

            self.storage.write(path, body.encode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            # A failure to write the log should never lose a post that
            # otherwise succeeded.
            log.warning("Could not write to the event log: %s", exc)

        return record

    def read(self, when: datetime | None = None) -> list[dict]:
        """Read one month of events, oldest first."""
        try:
            raw = self.storage.read(self.path_for(when or _now())).decode("utf-8")
        except FileNotFoundError:
            return []

        events = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # One malformed line should not hide the rest of the month.
                log.warning("Skipping unreadable log line.")
        return events
