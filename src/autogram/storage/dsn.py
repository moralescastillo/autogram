"""Connection-string parsing.

Deliberately hand-rolled rather than using ``urllib.parse``. The credentials
that appear in these strings are hostile to URL parsing: Google refresh tokens
start with ``1//`` and GCS HMAC secrets are base64, so they contain ``/``,
``+`` and ``=``. Splitting on the last ``@`` and a fixed number of leading
colons is unambiguous, where a URL parser would mangle them.
"""

from __future__ import annotations

from dataclasses import dataclass

from autogram.config import ConfigError


@dataclass(frozen=True)
class ParsedDSN:
    scheme: str
    credentials: list[str]
    location: str

    @property
    def bucket(self) -> str:
        """Bucket or root identifier — everything before the first slash."""
        return self.location.split("/", 1)[0]

    @property
    def prefix(self) -> str:
        """Optional path under the bucket, without leading or trailing slash."""
        _, _, rest = self.location.partition("/")
        return rest.strip("/")


def parse(dsn: str, *, expected_credentials: int) -> ParsedDSN:
    """Split a connection string into scheme, credentials and location.

    ``expected_credentials`` is the number of colon-separated credential fields
    the scheme takes. Only that many splits are performed, so a secret
    containing colons stays intact in the final field.
    """
    dsn = dsn.strip()

    scheme, sep, rest = dsn.partition("://")
    if not sep or not scheme:
        raise ConfigError(f"Malformed connection string: missing '://' in {_redact(dsn)}.")

    # Credentials may contain '@' (rare but legal in base64url), so split on
    # the *last* one — the location never contains an '@'.
    credentials_part, sep, location = rest.rpartition("@")
    if not sep:
        raise ConfigError(
            f"Malformed {scheme} connection string: expected "
            f"'{scheme}://<credentials>@<location>'."
        )

    location = location.strip("/")
    if not location:
        raise ConfigError(f"Malformed {scheme} connection string: no location after '@'.")

    credentials = credentials_part.split(":", expected_credentials - 1)
    if len(credentials) != expected_credentials or not all(credentials):
        raise ConfigError(
            f"Malformed {scheme} connection string: expected "
            f"{expected_credentials} colon-separated credential field(s) "
            f"before '@', found {len(credentials)}."
        )

    return ParsedDSN(scheme=scheme.lower(), credentials=credentials, location=location)


def _redact(dsn: str) -> str:
    """Render a connection string safe to put in an error message or log."""
    scheme, sep, rest = dsn.partition("://")
    if not sep:
        return "<malformed>"
    _, sep, location = rest.rpartition("@")
    return f"{scheme}://***@{location}" if sep else f"{scheme}://***"
