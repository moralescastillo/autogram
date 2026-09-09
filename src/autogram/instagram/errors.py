"""Graph API errors, and what the pipeline should do about them.

Meta returns errors as::

    {"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}

Every field is kept — ``fbtrace_id`` in particular, since it is the first thing
Meta support asks for.

The classification matters more than the message. Three outcomes, because the
pipeline does something genuinely different for each:

``AUTH``
    The token is dead or wrong. Refreshing might help; retrying will not.
    Nothing else can publish either, so this stops the run.

``RETRY_LATER``
    Rate limited, or Instagram had a bad moment. The post is untouched and the
    next hourly run tries again. No ``error.txt``: nothing is wrong with the
    user's content and telling them otherwise would be noise.

``FAIL_POST``
    This post cannot be published as it stands — media Instagram refused to
    fetch, a format it rejects, a container that expired. The user has to
    change something, so they get an ``error.txt`` explaining what.
"""

from __future__ import annotations

from enum import Enum


class Disposition(str, Enum):
    AUTH = "auth"
    RETRY_LATER = "retry_later"
    FAIL_POST = "fail_post"


#: Token problems. 190 covers expired, revoked and malformed tokens alike.
AUTH_CODES = {190, 102, 458, 463, 467}

#: Rate limiting and transient application-level throttles.
RATE_LIMIT_CODES = {4, 17, 32, 613}


class InstagramError(Exception):
    """A Graph API call failed."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        error_type: str | None = None,
        fbtrace_id: str | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id
        self.http_status = http_status

    @classmethod
    def from_response(cls, payload: dict, http_status: int) -> "InstagramError":
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        return cls(
            error.get("message") or f"HTTP {http_status} from Instagram",
            code=error.get("code"),
            subcode=error.get("error_subcode"),
            error_type=error.get("type"),
            fbtrace_id=error.get("fbtrace_id"),
            http_status=http_status,
        )

    @property
    def disposition(self) -> Disposition:
        """What the pipeline should do about this."""
        if self.code in AUTH_CODES:
            return Disposition.AUTH

        if self.code in RATE_LIMIT_CODES:
            return Disposition.RETRY_LATER

        if self.http_status is not None and self.http_status >= 500:
            return Disposition.RETRY_LATER

        # The 2207xxx family is media-related: unfetchable URL, unsupported
        # format, aspect ratio out of bounds, video too long. Matched by prefix
        # rather than enumerated — there are many and Meta adds more.
        if self.subcode is not None and 2207000 <= self.subcode < 2208000:
            return Disposition.FAIL_POST

        return Disposition.FAIL_POST

    @property
    def is_retryable(self) -> bool:
        return self.disposition is Disposition.RETRY_LATER

    def __str__(self) -> str:
        parts = [self.message]
        if self.code is not None:
            detail = f"code {self.code}"
            if self.subcode is not None:
                detail += f", subcode {self.subcode}"
            parts.append(f"({detail})")
        if self.fbtrace_id:
            parts.append(f"[fbtrace_id {self.fbtrace_id}]")
        return " ".join(parts)


class MediaNotReady(InstagramError):
    """A container did not finish processing inside the allowed time.

    Not a failure of the post — video processing is genuinely slow sometimes —
    so this retries rather than blaming the user's content.
    """

    @property
    def disposition(self) -> Disposition:
        return Disposition.RETRY_LATER
