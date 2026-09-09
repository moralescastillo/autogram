"""The Instagram Graph API client.

Uses the **Instagram API with Instagram Login** (``graph.instagram.com``),
which needs no linked Facebook Page. One client, one configurable API version —
the previous system accumulated two clients on two versions that drifted apart.

Publishing is always two steps, and never one:

1. ``POST /<ig_user_id>/media`` creates a *container* and returns its id
2. ``POST /<ig_user_id>/media_publish`` publishes that container

Between them, video containers must be polled until they report ``FINISHED``.
Images are usually ready on the first check.

**Media must already be at a public HTTPS URL.** The API takes ``image_url`` /
``video_url`` and fetches the bytes itself; there is no upload endpoint. This
is the whole reason an object store sits in the pipeline.

**On idempotency.** ``create_*`` returns a container id and ``publish`` takes
one, deliberately: the caller is expected to persist that id *before*
publishing. If a run dies between publishing and recording the fact, the next
run can ask ``container_status`` what happened — ``PUBLISHED`` means finish the
bookkeeping rather than post a duplicate. The client only exposes this; the
pipeline decides what to do with it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

from autogram.instagram.errors import InstagramError, MediaNotReady

log = logging.getLogger(__name__)

API_HOST = "https://graph.instagram.com"
DEFAULT_API_VERSION = "v25.0"

#: Meta suggests polling once a minute for five minutes. Large reels routinely
#: take longer than that, so the ceiling here is higher — a container that
#: never finishes costs one retry, while one abandoned too early costs a post.
POLL_INTERVAL_SECONDS = 5.0
POLL_MAX_INTERVAL_SECONDS = 30.0
POLL_TIMEOUT_SECONDS = 900.0  # 15 minutes

REQUEST_TIMEOUT_SECONDS = 60


class ContainerStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"
    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True)
class PublishedMedia:
    id: str
    permalink: str | None = None


class InstagramClient:
    def __init__(
        self,
        access_token: str,
        ig_user_id: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        session=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._token = access_token
        self.ig_user_id = ig_user_id
        self.api_version = api_version
        self._sleep = sleep
        self._clock = clock

        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    def __repr__(self) -> str:
        # A repr can end up in a log or a traceback; the token must not.
        return f"InstagramClient(ig_user_id={self.ig_user_id!r}, api_version={self.api_version!r})"

    # --- transport -----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{API_HOST}/{self.api_version}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **params) -> dict:
        """Make one Graph call, raising InstagramError on failure.

        The token goes in the Authorization header rather than the query
        string: URLs end up in logs, tracebacks and error reports, and a leaked
        long-lived token is a real incident.
        """
        params = {k: v for k, v in params.items() if v is not None}

        kwargs = {
            "headers": {"Authorization": f"Bearer {self._token}"},
            "timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if method == "GET":
            kwargs["params"] = params
        else:
            kwargs["data"] = params

        response = self._session.request(method, self._url(path), **kwargs)

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or "error" in payload:
            raise InstagramError.from_response(payload, response.status_code)

        return payload

    # --- containers ----------------------------------------------------

    def create_image_container(
        self,
        image_url: str,
        *,
        caption: str | None = None,
        alt_text: str | None = None,
        user_tags: list[dict] | None = None,
        location_id: str | None = None,
        is_carousel_item: bool = False,
    ) -> str:
        """Create a single-image container, or a carousel child.

        Carousel children carry no caption or alt text of their own — those
        belong to the parent container, and Instagram ignores them here.
        """
        params: dict = {"image_url": image_url}

        if is_carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption
            params["alt_text"] = alt_text
            params["location_id"] = location_id

        if user_tags:
            params["user_tags"] = json.dumps(user_tags)

        return self._create_container(params)

    def create_video_container(
        self,
        video_url: str,
        *,
        is_carousel_item: bool = False,
    ) -> str:
        """Create a video carousel child. Reels and stories have their own methods."""
        params: dict = {"video_url": video_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        return self._create_container(params)

    def create_carousel_container(
        self,
        children: list[str],
        *,
        caption: str | None = None,
        location_id: str | None = None,
    ) -> str:
        """Create the parent container for a carousel.

        ``children`` is ordered: the order given is the order Instagram shows,
        and the first child decides the aspect ratio every other item is
        cropped to.
        """
        if not 2 <= len(children) <= 10:
            raise ValueError(
                f"A carousel needs between 2 and 10 items, got {len(children)}."
            )

        return self._create_container(
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
                "location_id": location_id,
            }
        )

    def create_reel_container(
        self,
        video_url: str,
        *,
        caption: str | None = None,
        cover_url: str | None = None,
        thumb_offset: int | None = None,
        share_to_feed: bool = True,
        user_tags: list[dict] | None = None,
        location_id: str | None = None,
    ) -> str:
        """Create a reel container.

        ``user_tags`` for reels are usernames only — unlike images, they carry
        no coordinates.
        """
        params: dict = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "cover_url": cover_url,
            "thumb_offset": thumb_offset,
            "location_id": location_id,
            "share_to_feed": "true" if share_to_feed else "false",
        }

        if user_tags:
            params["user_tags"] = json.dumps(
                [{"username": tag["username"]} for tag in user_tags]
            )

        return self._create_container(params)

    def create_story_container(
        self, *, image_url: str | None = None, video_url: str | None = None
    ) -> str:
        """Create a story container. Stories take no caption."""
        if bool(image_url) == bool(video_url):
            raise ValueError("A story needs exactly one of image_url or video_url.")

        return self._create_container(
            {"media_type": "STORIES", "image_url": image_url, "video_url": video_url}
        )

    def _create_container(self, params: dict) -> str:
        response = self._request("POST", f"{self.ig_user_id}/media", **params)
        container_id = response.get("id")
        if not container_id:
            raise InstagramError(f"Instagram returned no container id: {response!r}")
        return container_id

    # --- status and publishing -----------------------------------------

    def container_status(self, container_id: str) -> ContainerStatus:
        """Ask Instagram what state a container is in."""
        response = self._request(
            "GET", container_id, fields="status_code"
        )
        raw = response.get("status_code")
        try:
            return ContainerStatus(raw)
        except ValueError:
            raise InstagramError(
                f"Unrecognised container status {raw!r} for {container_id}."
            ) from None

    def wait_until_ready(
        self, container_id: str, *, timeout: float = POLL_TIMEOUT_SECONDS
    ) -> None:
        """Block until a container is ready to publish.

        Images normally return FINISHED on the first check, so this costs one
        call. Video is genuinely asynchronous and can take minutes.
        """
        deadline = self._clock() + timeout
        interval = POLL_INTERVAL_SECONDS

        while True:
            status = self.container_status(container_id)

            if status is ContainerStatus.FINISHED:
                return

            if status is ContainerStatus.PUBLISHED:
                # Already live. Not an error — a previous run got further than
                # its bookkeeping suggests.
                log.warning("Container %s is already published.", container_id)
                return

            if status in (ContainerStatus.ERROR, ContainerStatus.EXPIRED):
                raise InstagramError(
                    f"Instagram could not process this media: container "
                    f"{container_id} reported {status.value}. This usually "
                    f"means the media could not be fetched, or its format, "
                    f"length or aspect ratio is not accepted."
                )

            if self._clock() >= deadline:
                raise MediaNotReady(
                    f"Container {container_id} was still processing after "
                    f"{timeout:.0f}s. This is usually a slow video rather than "
                    f"a problem with it; the next run will try again."
                )

            self._sleep(interval)
            interval = min(interval * 1.5, POLL_MAX_INTERVAL_SECONDS)

    def publish(self, container_id: str) -> PublishedMedia:
        """Publish a container that is ready.

        Persist ``container_id`` before calling this. If the process dies
        between here and recording success, that id is the only way to tell a
        published post from an unpublished one.
        """
        response = self._request(
            "POST", f"{self.ig_user_id}/media_publish", creation_id=container_id
        )
        media_id = response.get("id")
        if not media_id:
            raise InstagramError(f"Instagram returned no media id: {response!r}")
        return PublishedMedia(id=media_id)

    # --- account -------------------------------------------------------

    def whoami(self) -> dict:
        """Return the authenticated account, to prove a token works."""
        return self._request("GET", "me", fields="id,username")

    def publishing_limit(self) -> dict:
        """How much of the 100-posts-per-24h allowance is used."""
        return self._request(
            "GET",
            f"{self.ig_user_id}/content_publishing_limit",
            fields="config,quota_usage",
        )

    def refresh_access_token(self) -> tuple[str, int]:
        """Exchange the current long-lived token for a fresh 60 days.

        Returns the new token and its lifetime in seconds. The token must be at
        least 24 hours old, and one left unrefreshed for 60 days is dead for
        good — hence the wide safety margin in the scheduling logic.

        Note for whoever writes token persistence: a *bootstrap* token has
        unknown age. Attempting a refresh immediately is the way to find out —
        success gives an exact lifetime, while the "too young" error means the
        token is fresh and roughly 60 days remain.
        """
        response = self._session.get(
            f"{API_HOST}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": self._token},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or "error" in payload:
            raise InstagramError.from_response(payload, response.status_code)

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not token or expires_in is None:
            raise InstagramError(f"Malformed refresh response: {payload!r}")

        self._token = token
        return token, int(expires_in)
