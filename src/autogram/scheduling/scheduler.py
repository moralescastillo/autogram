"""Deciding whether to post right now.

The workflow runs hourly and each run asks one question: is a post due?

**Why the obvious approach fails.** The design originally said each run would
compute the target hour and check whether this is it — no state required.
That breaks on GitHub's scheduler, which fires late under load, sometimes by
an hour, and occasionally skips a tick. If today's target is 14:37 and the
14:00 run arrives at 15:03, an equality check misses the slot and the post
silently waits a whole cycle.

So the real test is three parts:

1. today is an eligible day, and
2. the local clock is at or past today's target time, and
3. nothing has been published today yet

The third part is what needs the ledger, and it is what makes a late run
correct rather than lost. A run at 15:03 for a 14:37 target still publishes;
the run after it does not.

**Why the target time is a hash.** Posting exactly on the hour looks
mechanical, so each day gets a pseudo-random minute inside the window. It is
derived from the date and account with SHA-256, which means every run on a
given day computes the same target without storing anything.

``hash()`` would not do: Python randomises string hashing per process, so two
runs on the same day would disagree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from autogram.scheduling.policy import Policy


@dataclass(frozen=True)
class Decision:
    """Whether to post, and a sentence explaining why."""

    due: bool
    reason: str
    target: datetime | None = None

    def __bool__(self) -> bool:
        return self.due


def target_time(policy: Policy, local_date: date, account_id: str) -> datetime:
    """The moment a post should go out on a given local day.

    Stable for a given (date, account): the same day always yields the same
    minute, so no state is needed to remember the choice. Including the account
    means two accounts scheduled alike do not post in lockstep.
    """
    seed = f"{account_id}:{local_date.isoformat()}".encode()
    digest = hashlib.sha256(seed).digest()

    span = max(policy.window.minutes, 1)
    offset = int.from_bytes(digest[:8], "big") % span

    start_minutes = policy.window.start.hour * 60 + policy.window.start.minute
    chosen = start_minutes + offset

    return datetime.combine(
        local_date,
        time(chosen // 60, chosen % 60),
        tzinfo=policy.timezone,
    )


def _is_eligible_day(
    policy: Policy, local_date: date, last_published: date | None
) -> tuple[bool, str]:
    cadence = policy.cadence

    if cadence.weekdays is not None:
        if local_date.weekday() in cadence.weekdays:
            return True, ""
        return False, f"{local_date:%A} is not a posting day ({cadence.describe()})"

    interval = cadence.interval_days or 1

    if last_published is None:
        # Nothing has ever been posted, so the first one is due immediately
        # rather than waiting for an arbitrary epoch to come round.
        return True, ""

    elapsed = (local_date - last_published).days
    if elapsed >= interval:
        return True, ""

    return False, (
        f"last posted {elapsed} day(s) ago; cadence is {cadence.describe()}"
    )


def next_slot(
    policy: Policy,
    after: datetime,
    account_id: str,
    last_published: datetime | None = None,
) -> datetime:
    """The next moment a post could go out, at or after ``after``.

    Used for reporting — a dry run says when the next post would land, which
    is the question a user actually has.
    """
    local_now = after.astimezone(policy.timezone)
    last_date = last_published.astimezone(policy.timezone).date() if last_published else None

    # A year is far beyond any sane cadence; the loop is bounded so a
    # misconfigured policy cannot hang a run.
    for offset in range(0, 366):
        candidate_date = local_now.date() + timedelta(days=offset)

        eligible, _ = _is_eligible_day(policy, candidate_date, last_date)
        if not eligible:
            continue

        target = target_time(policy, candidate_date, account_id)
        if target >= local_now:
            return target

        # Today's slot has passed. If nothing was published today, the post is
        # due now rather than tomorrow.
        if offset == 0 and last_date != candidate_date:
            return target

    return target_time(policy, local_now.date() + timedelta(days=366), account_id)


def is_due(
    policy: Policy,
    now: datetime,
    account_id: str,
    last_published: datetime | None = None,
) -> Decision:
    """Decide whether to publish on this run.

    ``last_published`` comes from the ledger. It is what makes this tolerant of
    a late or skipped run: the slot stays open for the rest of the day rather
    than being missed.
    """
    local_now = now.astimezone(policy.timezone)
    today = local_now.date()

    last_local = last_published.astimezone(policy.timezone) if last_published else None
    last_date = last_local.date() if last_local else None

    if last_date == today:
        return Decision(
            False,
            f"already published today at {last_local:%H:%M} {policy.timezone_name}",
            target=next_slot(policy, local_now, account_id, last_published),
        )

    eligible, why_not = _is_eligible_day(policy, today, last_date)
    if not eligible:
        return Decision(
            False,
            f"not due: {why_not}",
            target=next_slot(policy, local_now, account_id, last_published),
        )

    target = target_time(policy, today, account_id)

    if local_now < target:
        return Decision(
            False,
            f"not due yet: today's slot is {target:%H:%M} {policy.timezone_name}",
            target=target,
        )

    late_by = local_now - target
    detail = ""
    if late_by > timedelta(minutes=90):
        # Worth surfacing: it means the scheduler ran late, and the slot was
        # held open rather than missed.
        detail = f", running {late_by.seconds // 60}m late"

    return Decision(
        True,
        f"due: today's slot was {target:%H:%M} {policy.timezone_name}{detail}",
        target=target,
    )
