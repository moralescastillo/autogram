"""Config parsing — the one piece of real logic in the scaffold."""

import pytest

from autogram.config import ConfigError, load, parse_storage

GDRIVE = "gdrive://token@folder123"
GCS = "gs://key:secret@bucket/prefix"


def test_single_serving_backend_fills_both_roles():
    assert parse_storage(GCS) == (GCS, GCS)


def test_split_authoring_and_serving():
    assert parse_storage(f"{GDRIVE}\n{GCS}") == (GDRIVE, GCS)


def test_surrounding_whitespace_is_tolerated():
    # Secrets get pasted with stray newlines; that must not break setup.
    assert parse_storage(f"\n  {GDRIVE}  \n\n{GCS}\n\n") == (GDRIVE, GCS)


def test_authoring_only_backend_alone_is_rejected():
    # Drive cannot hand Instagram a URL, so this config could never publish.
    with pytest.raises(ConfigError, match="cannot serve"):
        parse_storage(GDRIVE)


def test_serving_slot_must_be_able_to_serve():
    with pytest.raises(ConfigError, match="cannot serve"):
        parse_storage(f"{GDRIVE}\ndropbox://token@/root")


def test_unknown_scheme_is_rejected():
    with pytest.raises(ConfigError, match="Unknown storage scheme"):
        parse_storage("ftp://nope")


def test_empty_is_rejected():
    with pytest.raises(ConfigError, match="empty"):
        parse_storage("   \n  ")


def test_too_many_lines_is_rejected():
    with pytest.raises(ConfigError, match="expected at most 2"):
        parse_storage(f"{GDRIVE}\n{GCS}\n{GCS}")


def test_load_reports_all_missing_secrets_at_once():
    with pytest.raises(ConfigError) as exc:
        load(env={})
    message = str(exc.value)
    assert "AUTOGRAM_STORAGE" in message
    assert "AUTOGRAM_IG_TOKEN" in message
    assert "AUTOGRAM_IG_USER_ID" in message


def test_load_builds_config():
    config = load(
        env={
            "AUTOGRAM_STORAGE": f"{GDRIVE}\n{GCS}",
            "AUTOGRAM_IG_TOKEN": "IGQ-bootstrap",
            "AUTOGRAM_IG_USER_ID": "17841400000000000",
        }
    )
    assert config.authoring_dsn == GDRIVE
    assert config.serving_dsn == GCS
    assert config.is_split
    assert not config.dry_run
