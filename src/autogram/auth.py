"""One-time credential setup.

Turning an OAuth client into a refresh token is the roughest part of getting
started, and the alternative — walking someone through Google's OAuth
Playground by hand — is error-prone and easy to abandon. This runs the consent
flow locally and prints the finished connection string.

Google blocks the old copy-paste ("out-of-band") flow, so this uses a loopback
redirect: a short-lived local web server catches the authorization code after
consent. That needs a browser on the same machine, which is why
``--no-browser`` exists for people running this over SSH.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

INSTAGRAM_SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]

INSTAGRAM_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH = "https://graph.instagram.com"


class AuthError(Exception):
    """Setup could not complete. The message is written for the user."""


def gdrive(
    client_id: str,
    client_secret: str,
    folder_id: str,
    *,
    use_browser: bool = True,
    port: int = 0,
) -> str:
    """Run Google's consent flow and return a ``gdrive://`` connection string.

    Requests offline access with a forced consent prompt: Google only returns a
    refresh token on first authorisation otherwise, so a user re-running this
    after a mistake would silently get a connection string with no token in it.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AuthError(
            "google-auth-oauthlib is not installed. Run: pip install -e ."
        ) from exc

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            # Loopback redirect — Google blocked the copy-paste flow in 2023.
            "redirect_uris": ["http://localhost"],
        }
    }

    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=DRIVE_SCOPES)

        if use_browser:
            credentials = flow.run_local_server(
                port=port,
                access_type="offline",
                prompt="consent",
                open_browser=True,
                authorization_prompt_message=(
                    "\nOpening your browser to authorise Autogram.\n"
                    "If it does not open, visit this URL:\n\n    {url}\n"
                ),
                success_message=(
                    "Authorised. You can close this tab and return to the terminal."
                ),
            )
        else:
            credentials = flow.run_local_server(
                port=port,
                access_type="offline",
                prompt="consent",
                open_browser=False,
                authorization_prompt_message=(
                    "\nOpen this URL in a browser — it can be on another "
                    "device, as long as that device can reach this machine "
                    "on the port below:\n\n    {url}\n"
                ),
            )
    except Exception as exc:
        raise AuthError(f"Authorisation failed: {exc}") from exc

    if not credentials.refresh_token:
        raise AuthError(
            "Google returned no refresh token. This usually means the app was "
            "already authorised. Remove Autogram at "
            "https://myaccount.google.com/permissions and run this again."
        )

    return f"gdrive://{client_id}:{client_secret}:{credentials.refresh_token}@{folder_id}"


def verify(dsn: str) -> str:
    """Check a connection string actually works, returning what it found.

    Failing here — at setup, with a clear message — is much kinder than failing
    at 3am inside a scheduled run.
    """
    from autogram.storage import from_dsn

    storage = from_dsn(dsn)
    entries = storage.list("")
    folders = [e.path for e in entries if e.is_dir]

    summary = f"Connected. {len(entries)} item(s) at the root."
    if folders:
        summary += f" Folders: {', '.join(sorted(folders)[:5])}"
    return summary


def instagram_authorize_url(client_id: str, redirect_uri: str) -> str:
    """The URL a user visits to authorise the app."""
    from urllib.parse import urlencode

    return f"{INSTAGRAM_AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
        }
    )


def instagram_exchange(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> tuple[str, str]:
    """Turn an authorization code into a long-lived token and the user id.

    Three steps, because Instagram makes it three: the code buys a token that
    lasts an hour, which must then be exchanged for the 60-day one Autogram
    actually stores.
    """
    import requests

    # Instagram appends "#_" to the redirected URL; pasting it verbatim is the
    # obvious thing to do, so accept it rather than failing on it.
    code = code.strip().removesuffix("#_")

    short = requests.post(
        INSTAGRAM_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    payload = _json_or_error(short, "exchanging the authorization code")

    short_token = payload.get("access_token")
    user_id = str(payload.get("user_id") or "")
    if not short_token:
        raise AuthError(f"No access token in Instagram's response: {payload!r}")

    long = requests.get(
        f"{INSTAGRAM_GRAPH}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    payload = _json_or_error(long, "exchanging for a long-lived token")

    token = payload.get("access_token")
    if not token:
        raise AuthError(f"No long-lived token in Instagram's response: {payload!r}")

    if not user_id:
        user_id = instagram_user_id(token)

    return token, user_id


def instagram_user_id(access_token: str) -> str:
    """Ask Instagram which account a token belongs to."""
    import requests

    response = requests.get(
        f"{INSTAGRAM_GRAPH}/me",
        params={"fields": "user_id,username", "access_token": access_token},
        timeout=30,
    )
    payload = _json_or_error(response, "looking up the account")
    return str(payload.get("user_id") or payload.get("id") or "")


def instagram_check(access_token: str) -> dict:
    """Confirm a token works and say whose account it is."""
    import requests

    response = requests.get(
        f"{INSTAGRAM_GRAPH}/me",
        params={"fields": "user_id,username", "access_token": access_token},
        timeout=30,
    )
    return _json_or_error(response, "checking the token")


def _json_or_error(response, what: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise AuthError(
            f"Instagram returned something unreadable while {what} "
            f"(HTTP {response.status_code})."
        ) from None

    if not response.ok or "error" in payload:
        error = payload.get("error") or payload.get("error_message") or payload
        if isinstance(error, dict):
            detail = error.get("message", str(error))
            hint = ""
            if error.get("code") == 190:
                hint = " The authorization code may have expired — they last one hour and work only once."
            raise AuthError(f"Instagram rejected the request while {what}: {detail}.{hint}")
        raise AuthError(f"Instagram rejected the request while {what}: {error}")

    return payload
