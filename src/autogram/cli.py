"""Command-line entry point.

    autogram auth gdrive --client-id ... --client-secret ... --folder-id ...
    autogram auth verify "gdrive://..."
    autogram run [--dry-run]

``--dry-run`` walks the whole pipeline — discovery, validation, scheduling,
staging decisions — and reports what would be published without calling
Instagram or moving anything. Worth using before every real change; the
previous system had it on every stage and it earned its keep.
"""

from __future__ import annotations

import argparse
import logging
import sys

from autogram import __version__
from autogram.config import ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autogram",
        description="Publish scheduled content to Instagram from your own storage.",
    )
    parser.add_argument("--version", action="version", version=f"autogram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Do one scheduling pass and publish if due.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without publishing or moving anything.",
    )
    run.add_argument("--verbose", "-v", action="store_true", help="Log every step.")

    auth = sub.add_parser("auth", help="Set up credentials. Run once.")
    auth_sub = auth.add_subparsers(dest="provider", required=True)

    gdrive = auth_sub.add_parser(
        "gdrive",
        help="Authorise Google Drive and print its connection string.",
        description=(
            "Runs Google's consent flow in your browser and prints the "
            "connection string to paste into the AUTOGRAM_STORAGE secret. "
            "Create an OAuth client of type 'Desktop app' first — see "
            "docs/SETUP.md."
        ),
    )
    gdrive.add_argument("--client-id", required=True, help="OAuth client ID.")
    gdrive.add_argument("--client-secret", required=True, help="OAuth client secret.")
    gdrive.add_argument(
        "--folder-id",
        required=True,
        help="Drive folder ID — the last part of the folder's URL.",
    )
    gdrive.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the URL instead of opening a browser (for remote machines).",
    )
    gdrive.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for the local callback server. Default: any free port.",
    )

    instagram = auth_sub.add_parser(
        "instagram",
        help="Turn an Instagram authorization code into a long-lived token.",
        description=(
            "Exchanges an authorization code for the 60-day token Autogram "
            "stores, and reports the account it belongs to. Run without --code "
            "to print the URL to visit first. If your app dashboard offers a "
            "'Generate token' button, use that instead and check the result "
            "with 'autogram auth instagram --token <token>'."
        ),
    )
    instagram.add_argument("--client-id", help="Instagram app ID.")
    instagram.add_argument("--client-secret", help="Instagram app secret.")
    instagram.add_argument(
        "--redirect-uri",
        help="A redirect URI configured on the app. Must match exactly.",
    )
    instagram.add_argument(
        "--code", help="The code from the redirect URL after you authorise."
    )
    instagram.add_argument(
        "--token",
        help="Check an existing long-lived token instead of exchanging a code.",
    )

    verify = auth_sub.add_parser(
        "verify", help="Check a connection string works before relying on it."
    )
    verify.add_argument("dsn", help="The connection string to test.")

    return parser


def _instagram_auth(args) -> int:
    from autogram.auth import (
        AuthError,
        instagram_authorize_url,
        instagram_check,
        instagram_exchange,
    )

    if args.token:
        account = instagram_check(args.token)
        print(f"\nToken works — @{account.get('username', '?')}\n")
        print("Add these as GitHub secrets:\n")
        print(f"  AUTOGRAM_IG_TOKEN     {args.token}")
        print(f"  AUTOGRAM_IG_USER_ID   {account.get('user_id') or account.get('id')}\n")
        return 0

    missing = [
        name
        for name in ("client_id", "client_secret", "redirect_uri")
        if not getattr(args, name)
    ]
    if missing:
        raise AuthError(
            "Need --client-id, --client-secret and --redirect-uri "
            f"(missing: {', '.join('--' + m.replace('_', '-') for m in missing)})."
        )

    if not args.code:
        # Without a code there is nothing to exchange, so hand over the URL
        # that produces one rather than failing.
        print("\n1. Open this URL and authorise the app:\n")
        print(f"   {instagram_authorize_url(args.client_id, args.redirect_uri)}\n")
        print("2. You will be redirected to a URL containing '?code=...'.")
        print("   Copy that code — it lasts one hour and works only once.\n")
        print("3. Run this command again with --code <the code>\n")
        return 0

    token, user_id = instagram_exchange(
        args.client_id, args.client_secret, args.code, args.redirect_uri
    )
    account = instagram_check(token)

    print(f"\nAuthorised as @{account.get('username', '?')}\n")
    print("Add these as GitHub secrets:\n")
    print(f"  AUTOGRAM_IG_TOKEN     {token}")
    print(f"  AUTOGRAM_IG_USER_ID   {user_id}\n")
    print("The token lasts 60 days; Autogram refreshes it for you from here on.\n")
    return 0


def _run_auth(args) -> int:
    from autogram.auth import AuthError, gdrive, verify

    try:
        if args.provider == "gdrive":
            dsn = gdrive(
                client_id=args.client_id,
                client_secret=args.client_secret,
                folder_id=args.folder_id,
                use_browser=not args.no_browser,
                port=args.port,
            )
            print("\nChecking the connection...")
            print(f"  {verify(dsn)}\n")
            print("Add this as the first line of your AUTOGRAM_STORAGE secret:\n")
            print(f"  {dsn}\n")
            print(
                "Keep it secret — it grants access to your Drive. If it leaks, "
                "revoke it at https://myaccount.google.com/permissions\n"
            )
            return 0

        if args.provider == "instagram":
            return _instagram_auth(args)

        if args.provider == "verify":
            print(verify(args.dsn))
            return 0

    except AuthError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        if args.command == "run":
            from autogram.run import run as do_run

            return do_run(dry_run=args.dry_run)

        if args.command == "auth":
            return _run_auth(args)

    except ConfigError as exc:
        # Configuration problems are the user's to fix, so they get a plain
        # message rather than a traceback.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
