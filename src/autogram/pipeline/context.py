"""Everything one run needs, assembled once.

Passing this around rather than a dozen arguments keeps the pipeline readable,
and makes ``dry_run`` a property of the run rather than a flag threaded through
every function.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from autogram.instagram.client import InstagramClient
from autogram.scheduling.policy import Policy
from autogram.state.ledger import Ledger
from autogram.state.log import EventLog
from autogram.state.pending import PendingStore
from autogram.storage.base import Storage

QUEUE_ROOT = "queue"
NOW_ROOT = "now"
PUBLISHED_ROOT = "published"
STAGING_ROOT = "staging"
ERROR_FILE = "error.txt"


@dataclass
class Context:
    authoring: Storage
    serving: Storage
    client: InstagramClient
    ledger: Ledger
    pending: PendingStore
    events: EventLog
    policy: Policy
    ig_user_id: str
    now: datetime
    dry_run: bool = False

    def staging_path(self, post_name: str, filename: str) -> str:
        return f"{STAGING_ROOT}/{post_name}/{filename}"

    def error_path(self, folder: str) -> str:
        return f"{folder.rstrip('/')}/{ERROR_FILE}"
