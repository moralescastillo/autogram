"""The auth command.

The consent flow itself needs a browser and a real Google client, so it is not
exercised here. What is testable — and what would actually break setup — is the
argument wiring, the connection string that comes out, and the error messages a
user would hit.
"""

import pytest

from autogram.auth import AuthError, DRIVE_SCOPES, gdrive
from autogram.cli import build_parser


class TestScopes:
    def test_requests_full_drive_scope(self):
        # drive.file cannot see folders the user made by hand on their phone,
        # which is the entire workflow. See DESIGN.md §6.2.1.
        assert DRIVE_SCOPES == ["https://www.googleapis.com/auth/drive"]


class TestCLIWiring:
    def test_gdrive_requires_client_and_folder(self):
        parser = build_parser()
        args = parser.parse_args([
            "auth", "gdrive",
            "--client-id", "id.apps.googleusercontent.com",
            "--client-secret", "GOCSPX-secret",
            "--folder-id", "folder123",
        ])
        assert args.command == "auth"
        assert args.provider == "gdrive"
        assert args.folder_id == "folder123"
        assert not args.no_browser
        assert args.port == 0

    def test_no_browser_flag(self):
        args = build_parser().parse_args([
            "auth", "gdrive",
            "--client-id", "x", "--client-secret", "y", "--folder-id", "z",
            "--no-browser",
        ])
        assert args.no_browser

    def test_missing_arguments_are_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["auth", "gdrive", "--client-id", "x"])

    def test_verify_takes_a_dsn(self):
        args = build_parser().parse_args(["auth", "verify", "gdrive://a:b:c@d"])
        assert args.provider == "verify"
        assert args.dsn == "gdrive://a:b:c@d"

    def test_provider_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["auth"])


class TestConnectionStringAssembly:
    def test_dsn_is_built_from_the_flow_result(self, monkeypatch):
        class FakeFlow:
            def run_local_server(self, **kwargs):
                # Both are required: without offline access Google returns no
                # refresh token, and without a forced prompt a re-run of this
                # command silently returns none either.
                assert kwargs["access_type"] == "offline"
                assert kwargs["prompt"] == "consent"
                return type("Creds", (), {"refresh_token": "1//0gTOKEN/with+chars="})()

        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
            lambda config, scopes: FakeFlow(),
        )

        dsn = gdrive("client-id", "client-secret", "folder-abc")
        assert dsn == "gdrive://client-id:client-secret:1//0gTOKEN/with+chars=@folder-abc"

    def test_dsn_round_trips_through_the_parser(self, monkeypatch):
        # The string this command prints must parse back correctly, tokens with
        # slashes and padding included.
        from autogram.storage.dsn import parse

        token = "1//0gK3-x/y+z="
        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
            lambda config, scopes: type("F", (), {
                "run_local_server": lambda self, **kw: type(
                    "C", (), {"refresh_token": token}
                )()
            })(),
        )

        dsn = gdrive("id", "secret", "folder")
        parsed = parse(dsn, expected_credentials=3)
        assert parsed.credentials == ["id", "secret", token]
        assert parsed.location == "folder"

    def test_missing_refresh_token_explains_the_fix(self, monkeypatch):
        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
            lambda config, scopes: type("F", (), {
                "run_local_server": lambda self, **kw: type(
                    "C", (), {"refresh_token": None}
                )()
            })(),
        )

        with pytest.raises(AuthError, match="already authorised"):
            gdrive("id", "secret", "folder")

    def test_flow_failure_is_reported_not_raised_raw(self, monkeypatch):
        def explode(config, scopes):
            raise RuntimeError("port already in use")

        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config", explode
        )

        with pytest.raises(AuthError, match="port already in use"):
            gdrive("id", "secret", "folder")


class TestInstagramAuth:
    def test_authorize_url_requests_publishing_scope(self):
        from autogram.auth import instagram_authorize_url

        url = instagram_authorize_url("12345", "https://example.com/cb")

        assert url.startswith("https://www.instagram.com/oauth/authorize?")
        assert "instagram_business_content_publish" in url
        assert "instagram_business_basic" in url
        assert "response_type=code" in url

    def test_exchange_returns_long_lived_token_and_user_id(self, monkeypatch):
        posted, fetched = {}, {}

        class Resp:
            def __init__(self, payload, ok=True):
                self._p, self.ok, self.status_code = payload, ok, 200 if ok else 400

            def json(self):
                return self._p

        def fake_post(url, data=None, timeout=None):
            posted.update(data)
            return Resp({"access_token": "SHORT", "user_id": 17841400000000000})

        def fake_get(url, params=None, timeout=None):
            fetched.update(params)
            if "access_token" in url:
                return Resp({"access_token": "LONG-LIVED", "expires_in": 5184000})
            return Resp({"user_id": "17841400000000000", "username": "me"})

        import requests

        monkeypatch.setattr(requests, "post", fake_post)
        monkeypatch.setattr(requests, "get", fake_get)

        from autogram.auth import instagram_exchange

        token, user_id = instagram_exchange("id", "secret", "CODE", "https://cb")

        assert token == "LONG-LIVED"
        assert user_id == "17841400000000000"
        assert posted["grant_type"] == "authorization_code"
        assert fetched["grant_type"] == "ig_exchange_token"

    def test_trailing_hash_in_pasted_code_is_tolerated(self, monkeypatch):
        # Instagram appends "#_" to the redirect; pasting it verbatim is the
        # obvious thing to do and must not fail.
        seen = {}

        class Resp:
            ok, status_code = True, 200

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        import requests

        monkeypatch.setattr(
            requests, "post",
            lambda url, data=None, timeout=None: (
                seen.update(data), Resp({"access_token": "S", "user_id": 1})
            )[1],
        )
        monkeypatch.setattr(
            requests, "get",
            lambda url, params=None, timeout=None: Resp({"access_token": "L"}),
        )

        from autogram.auth import instagram_exchange

        instagram_exchange("id", "secret", "ABC123#_", "https://cb")
        assert seen["code"] == "ABC123"

    def test_expired_code_explains_itself(self, monkeypatch):
        class Resp:
            ok, status_code = False, 400

            def json(self):
                return {"error": {"message": "Invalid code", "code": 190}}

        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())

        from autogram.auth import AuthError, instagram_exchange

        with pytest.raises(AuthError, match="expired"):
            instagram_exchange("id", "secret", "STALE", "https://cb")

    def test_cli_accepts_the_instagram_flow(self):
        args = build_parser().parse_args([
            "auth", "instagram",
            "--client-id", "123", "--client-secret", "s",
            "--redirect-uri", "https://cb", "--code", "ABC",
        ])
        assert args.provider == "instagram"
        assert args.code == "ABC"

    def test_cli_accepts_token_check(self):
        args = build_parser().parse_args(["auth", "instagram", "--token", "IGQ..."])
        assert args.token == "IGQ..."
