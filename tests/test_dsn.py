"""Connection-string parsing.

The credentials in these strings are hostile to URL parsing — Google refresh
tokens begin ``1//`` and HMAC secrets are base64 — so the fixtures here use
realistically shaped values rather than tidy placeholders.
"""

import pytest

from autogram.config import ConfigError
from autogram.storage.dsn import _redact, parse

# Shapes taken from real credentials, with the values replaced.
REFRESH_TOKEN = "1//0gK3xAmpLe-tOkEn_with/slashes+and=padding"
HMAC_SECRET = "aB3/xY9+zQ1kL7mN2pR5sT8uV0wX4yZ6cD1eF3gH="


class TestObjectStoreDSN:
    def test_parses_key_secret_bucket_and_prefix(self):
        parsed = parse(f"gs://GOOG1EXAMPLE:{HMAC_SECRET}@my-bucket/autogram", expected_credentials=2)
        assert parsed.scheme == "gs"
        assert parsed.credentials == ["GOOG1EXAMPLE", HMAC_SECRET]
        assert parsed.bucket == "my-bucket"
        assert parsed.prefix == "autogram"

    def test_secret_containing_slashes_survives(self):
        # urlparse would treat these as path separators.
        parsed = parse(f"gs://key:{HMAC_SECRET}@bucket", expected_credentials=2)
        assert parsed.credentials[1] == HMAC_SECRET

    def test_prefix_is_optional(self):
        parsed = parse("s3://key:secret@bucket", expected_credentials=2)
        assert parsed.bucket == "bucket"
        assert parsed.prefix == ""

    def test_nested_prefix(self):
        parsed = parse("s3://key:secret@bucket/a/b/c", expected_credentials=2)
        assert parsed.bucket == "bucket"
        assert parsed.prefix == "a/b/c"


class TestDriveDSN:
    def test_parses_three_credentials(self):
        dsn = f"gdrive://123.apps.googleusercontent.com:GOCSPX-secret:{REFRESH_TOKEN}@folder123"
        parsed = parse(dsn, expected_credentials=3)
        assert parsed.credentials == [
            "123.apps.googleusercontent.com",
            "GOCSPX-secret",
            REFRESH_TOKEN,
        ]
        assert parsed.location == "folder123"

    def test_refresh_token_with_slashes_survives(self):
        # Google refresh tokens start "1//" — the parser must not split there.
        parsed = parse(f"gdrive://id:secret:{REFRESH_TOKEN}@folder", expected_credentials=3)
        assert parsed.credentials[2] == REFRESH_TOKEN


class TestMalformed:
    def test_missing_scheme_separator(self):
        with pytest.raises(ConfigError, match="missing '://'"):
            parse("gs:key:secret@bucket", expected_credentials=2)

    def test_missing_at_sign(self):
        with pytest.raises(ConfigError, match="expected"):
            parse("gs://key:secret-bucket", expected_credentials=2)

    def test_missing_location(self):
        with pytest.raises(ConfigError, match="no location"):
            parse("gs://key:secret@", expected_credentials=2)

    def test_too_few_credentials(self):
        with pytest.raises(ConfigError, match="expected 3"):
            parse("gdrive://id:secret@folder", expected_credentials=3)

    def test_empty_credential_field(self):
        with pytest.raises(ConfigError, match="expected 2"):
            parse("gs://key:@bucket", expected_credentials=2)


class TestRedaction:
    def test_credentials_never_appear(self):
        redacted = _redact(f"gs://key:{HMAC_SECRET}@bucket/prefix")
        assert HMAC_SECRET not in redacted
        assert "key" not in redacted
        assert "bucket/prefix" in redacted

    def test_malformed_input_is_safe(self):
        assert _redact("nonsense") == "<malformed>"

    def test_error_messages_do_not_leak_credentials(self):
        # A leaked secret in a public CI log would be a real incident.
        with pytest.raises(ConfigError) as exc:
            parse(f"gs:key:{HMAC_SECRET}@bucket", expected_credentials=2)
        assert HMAC_SECRET not in str(exc.value)
