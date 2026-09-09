"""Instagram Graph API client.

One client, one configurable API version. Publishing is always two steps —
create a media container, then publish it — and video containers must be polled
until they report FINISHED (DESIGN.md §8.5).

Uses the Instagram API with Instagram Login (``graph.instagram.com``), which
needs no linked Facebook Page. Tokens are long-lived and expire after 60 days;
the client can refresh them, while deciding *when* to belongs to
``autogram.state``.
"""

from autogram.instagram.client import (
    ContainerStatus,
    InstagramClient,
    PublishedMedia,
)
from autogram.instagram.errors import Disposition, InstagramError, MediaNotReady

__all__ = [
    "ContainerStatus",
    "Disposition",
    "InstagramClient",
    "InstagramError",
    "MediaNotReady",
    "PublishedMedia",
]
