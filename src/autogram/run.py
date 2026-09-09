"""The pipeline: one pass, executed hourly.

The order matters and is worth stating, because most of the safety properties
come from it rather than from any single component:

1. Load config and the live token; refresh it if expiry is near (§4).
2. Look in ``now/`` first — anything there publishes immediately, ignoring
   cadence (§7.3).
3. Otherwise ask the schedule whether this is the hour (§7.2). If not, stop.
4. Discover the next queued post and validate it *fully* before uploading
   anything (§5.4).
5. Check the ledger — a post already published is never published twice (§8.1).
6. Stage media to the serving backend, publish, poll until FINISHED (§8.5).
7. Move the folder to ``published/``, record it, clean up staged media.

Failures at any step write ``error.txt`` into the post folder and a record to
the log, leaving the post in place to retry (§8.3).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run(*, dry_run: bool = False) -> int:
    """Execute one pass. Returns a process exit code."""
    raise NotImplementedError("Pipeline not yet implemented; see DESIGN.md §3.")
