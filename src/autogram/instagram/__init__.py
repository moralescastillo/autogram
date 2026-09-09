"""Instagram Graph API client.

One client, one configurable API version. Publishing is always two steps —
create a media container, then publish it — and video containers must be polled
until they report FINISHED (DESIGN.md §8.5).

Uses the Instagram API with Instagram Login (``graph.instagram.com``), which
needs no linked Facebook Page. Tokens are long-lived and expire after 60 days;
refresh is handled in ``autogram.state.token``.
"""
