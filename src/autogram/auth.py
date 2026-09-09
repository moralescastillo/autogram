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
