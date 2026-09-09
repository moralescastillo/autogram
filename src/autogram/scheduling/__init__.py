"""When to post.

The workflow runs hourly and each run asks whether a post is due. The answer
depends on three things: whether today is an eligible day, whether the local
clock has passed the day's target time, and whether anything has been published
today already.

The target time is derived from the date and account with SHA-256, so every run
on a given day agrees on it without storing anything — while the "already
published today" check comes from the ledger, and is what keeps a late or
skipped run from losing the slot (DESIGN.md §7.2).
"""

from autogram.scheduling.policy import Policy, PolicyError
from autogram.scheduling.policy import parse as parse_policy
from autogram.scheduling.scheduler import Decision, is_due, next_slot, target_time

__all__ = [
    "Decision",
    "Policy",
    "PolicyError",
    "is_due",
    "next_slot",
    "parse_policy",
    "target_time",
]
