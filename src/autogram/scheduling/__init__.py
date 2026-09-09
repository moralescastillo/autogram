"""When to post.

The workflow runs hourly. Each run decides whether this is the hour, using a
seed derived from the date and account — so the same day always picks the same
time, no state is needed, and a post cannot fire twice (DESIGN.md §7.2).
"""
