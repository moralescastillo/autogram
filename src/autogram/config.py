"""Configuration, loaded from environment variables.

Three secrets, no more (DESIGN.md §10):

- ``AUTOGRAM_STORAGE``    — one or two connection strings
- ``AUTOGRAM_IG_TOKEN``   — bootstrap Instagram token, read once (§4)
- ``AUTOGRAM_IG_USER_ID`` — the Instagram professional account id

Posting policy (cadence, window, timezone) is *not* here. It lives in
``autogram.yml`` at the storage root, so the user edits it from their phone
alongside their content rather than through GitHub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Recognised connection-string schemes and whether they can serve media.
#:
#: Expected shapes::
#:
#:     gdrive://<client_id>:<client_secret>:<refresh_token>@<folder_id>
#:     gs://<access_key>:<secret>@<bucket>/<prefix>
#:     s3://<access_key>:<secret>@<bucket>/<prefix>
#:     dropbox://<refresh_token>@<root_path>
#:
#: Drive needs the client id and secret as well as the refresh token: Google
#: will not exchange a refresh token for an access token without the
#: credentials of the OAuth client that issued it.
SCHEMES = {
    "gdrive": {"authoring": True, "serving": False},
    "dropbox": {"authoring": True, "serving": False},
    "gs": {"authoring": True, "serving": True},
    "s3": {"authoring": True, "serving": True},
    # Development and testing only: Instagram cannot fetch from a local disk.
    "local": {"authoring": True, "serving": False},
}


class ConfigError(Exception):
    """Configuration is missing or malformed. Message is shown to the user."""


@dataclass(frozen=True)
class Config:
    authoring_dsn: str
    serving_dsn: str
    ig_bootstrap_token: str
    ig_user_id: str
    dry_run: bool = False

    @property
    def is_split(self) -> bool:
        """True when authoring and serving use different backends."""
        return self.authoring_dsn != self.serving_dsn


def _scheme(dsn: str) -> str:
    scheme = dsn.split("://", 1)[0].strip().lower()
    if scheme not in SCHEMES:
        raise ConfigError(
            f"Unknown storage scheme {scheme!r}. "
            f"Expected one of: {', '.join(sorted(SCHEMES))}."
        )
    return scheme


def parse_storage(raw: str) -> tuple[str, str]:
    """Parse AUTOGRAM_STORAGE into (authoring_dsn, serving_dsn).

    One connection string per line. The first is the authoring backend, the
    second — if present — is the serving backend. Blank lines are ignored, so a
    secret pasted with a trailing newline behaves.

    A single string that can serve fills both roles. A single string that
    cannot (Drive, Dropbox) is an error, because there would be no way to hand
    Instagram a URL.
    """
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        raise ConfigError("AUTOGRAM_STORAGE is empty.")
    if len(lines) > 2:
        raise ConfigError(
            f"AUTOGRAM_STORAGE has {len(lines)} lines; expected at most 2 "
            "(authoring, then serving)."
        )

    authoring = lines[0]
    _scheme(authoring)

    if len(lines) == 2:
        serving = lines[1]
        if not SCHEMES[_scheme(serving)]["serving"]:
            raise ConfigError(
                f"{_scheme(serving)} cannot serve media to Instagram. "
                "Use gs:// or s3:// as the second line."
            )
        return authoring, serving

    if not SCHEMES[_scheme(authoring)]["serving"]:
        # Not fatal here: `autogram run --dry-run` can still validate content
        # with no serving backend, and the run itself refuses to publish.
        return authoring, authoring

    return authoring, authoring


def load(env: dict[str, str] | None = None, *, dry_run: bool = False) -> Config:
    """Build a Config from the environment, or raise ConfigError."""
    env = dict(os.environ if env is None else env)

    missing = [
        name
        for name in ("AUTOGRAM_STORAGE", "AUTOGRAM_IG_TOKEN", "AUTOGRAM_IG_USER_ID")
        if not env.get(name, "").strip()
    ]
    if missing:
        raise ConfigError(f"Missing required secrets: {', '.join(missing)}.")

    authoring, serving = parse_storage(env["AUTOGRAM_STORAGE"])
    return Config(
        authoring_dsn=authoring,
        serving_dsn=serving,
        ig_bootstrap_token=env["AUTOGRAM_IG_TOKEN"].strip(),
        ig_user_id=env["AUTOGRAM_IG_USER_ID"].strip(),
        dry_run=dry_run,
    )
