"""State: token, ledger, event log and the in-flight marker.

All against real LocalStorage with ``now`` injected, so the 24-hour gates and
60-day expiries are exercised exactly rather than waited for.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from autogram.instagram.errors import InstagramError
from autogram.state.ledger import Ledger, LedgerCorrupt
from autogram.state.log import EventLog, PUBLISHED, TOKEN_REFRESHED
from autogram.state.pending import PendingStore
from autogram.state.token import (
    ASSUMED_LIFETIME,
    TokenInfo,
    TokenStore,
)
from autogram.storage.local import LocalStorage

ACCOUNT = "17841400000000000"
BOOTSTRAP = "IGQ-bootstrap-token"


def at(day, hour=12) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path)


class FakeClient:
    """Stands in for InstagramClient's refresh call."""

    def __init__(self, token="NEW-TOKEN", expires_in=5184000, error=None):
        self.token = token
        self.expires_in = expires_in
        self.error = error
        self.calls = 0

    def refresh_access_token(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.token, self.expires_in


class TestTokenSeeding:
    def test_seeds_from_bootstrap_when_nothing_stored(self, storage):
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))

        assert info.access_token == BOOTSTRAP
        assert info.expires_at == at(1) + ASSUMED_LIFETIME
        assert not info.confirmed  # the age of a pasted token is unknowable

    def test_load_does_not_write(self, storage):
        # A dry run must be able to inspect the token without changing it.
        store = TokenStore(storage)
        store.load(BOOTSTRAP, ACCOUNT, now=at(1))

        with pytest.raises(FileNotFoundError):
            storage.read("state/token.json")

    def test_stored_token_is_preferred_over_bootstrap(self, storage):
        store = TokenStore(storage)
        stored = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        stored.access_token = "REFRESHED"
        store.save(stored)

        assert store.load(BOOTSTRAP, ACCOUNT, now=at(2)).access_token == "REFRESHED"

    def test_changed_bootstrap_reseeds(self, storage):
        # How a user recovers from a dead token: paste a new secret.
        store = TokenStore(storage)
        store.save(store.load(BOOTSTRAP, ACCOUNT, now=at(1)))

        info = store.load("A-DIFFERENT-TOKEN", ACCOUNT, now=at(2))
        assert info.access_token == "A-DIFFERENT-TOKEN"
        assert not info.confirmed

    def test_changed_account_reseeds(self, storage):
        store = TokenStore(storage)
        store.save(store.load(BOOTSTRAP, ACCOUNT, now=at(1)))

        info = store.load(BOOTSTRAP, "99999999", now=at(2))
        assert info.ig_user_id == "99999999"

    def test_unreadable_token_file_reseeds(self, storage):
        storage.write("state/token.json", b"{ this is not json")
        info = TokenStore(storage).load(BOOTSTRAP, ACCOUNT, now=at(1))
        assert info.access_token == BOOTSTRAP

    def test_bootstrap_is_never_stored_in_plaintext_fingerprint(self, storage):
        store = TokenStore(storage)
        store.save(store.load(BOOTSTRAP, ACCOUNT, now=at(1)))

        raw = storage.read("state/token.json").decode()
        assert BOOTSTRAP in raw  # the token itself is needed
        # but the fingerprint must not be reversible to it
        assert json.loads(raw)["bootstrap_fingerprint"] != BOOTSTRAP


class TestTokenRefresh:
    def test_too_young_to_refresh(self, storage):
        # Meta refuses tokens under 24 hours old.
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        client = FakeClient()

        info, changed = store.refresh_if_needed(info, client, now=at(1, hour=20))

        assert not changed
        assert client.calls == 0

    def test_confirms_the_expiry_once_old_enough(self, storage):
        # The seeded 60 days is a guess; this replaces it with Meta's answer.
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        client = FakeClient(expires_in=5184000)

        info, changed = store.refresh_if_needed(info, client, now=at(3))

        assert changed
        assert info.confirmed
        assert info.access_token == "NEW-TOKEN"
        assert info.days_remaining(at(3)) == pytest.approx(60, abs=0.1)

    def test_confirmed_token_is_left_alone_until_the_threshold(self, storage):
        store = TokenStore(storage)
        info = TokenInfo(
            access_token="LIVE",
            expires_at=at(1) + timedelta(days=60),
            refreshed_at=at(1),
            confirmed=True,
        )
        client = FakeClient()

        info, changed = store.refresh_if_needed(info, client, now=at(20))

        assert not changed
        assert client.calls == 0

    def test_refreshes_inside_the_threshold(self, storage):
        store = TokenStore(storage)
        info = TokenInfo(
            access_token="LIVE",
            expires_at=at(10),
            refreshed_at=at(1),
            confirmed=True,
        )

        info, changed = store.refresh_if_needed(info, FakeClient(), now=at(5))

        assert changed
        assert info.access_token == "NEW-TOKEN"

    def test_refreshed_token_is_persisted(self, storage):
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        store.refresh_if_needed(info, FakeClient(), now=at(3))

        # In memory but not on disk would be lost at the end of the run.
        assert store.load(BOOTSTRAP, ACCOUNT, now=at(4)).access_token == "NEW-TOKEN"

    def test_auth_failure_is_fatal(self, storage):
        # Nothing can publish with a dead token, so this must not be swallowed.
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        client = FakeClient(error=InstagramError("expired", code=190))

        with pytest.raises(InstagramError):
            store.refresh_if_needed(info, client, now=at(3))

    def test_transient_failure_is_not_fatal(self, storage):
        # Days of margin remain; failing the run would stop a valid post.
        store = TokenStore(storage)
        info = TokenInfo(
            access_token="LIVE",
            expires_at=at(10),
            refreshed_at=at(1),
            confirmed=True,
        )
        client = FakeClient(error=InstagramError("rate limited", code=4))

        info, changed = store.refresh_if_needed(info, client, now=at(5))

        assert not changed
        assert info.access_token == "LIVE"

    def test_seed_is_persisted_so_its_age_accumulates(self, storage):
        # load() never writes, so an in-memory-only seed would reset its own
        # age on every run: the 24-hour gate would never open, the token would
        # never be confirmed or refreshed, and it would silently die at 60 days.
        store = TokenStore(storage)
        info = store.load(BOOTSTRAP, ACCOUNT, now=at(1))
        store.refresh_if_needed(info, FakeClient(), now=at(1))

        assert store.load(BOOTSTRAP, ACCOUNT, now=at(1)).seeded_at == at(1)

    def test_token_survives_a_full_year_of_hourly_runs(self, storage):
        # The failure this guards against is the one the previous system had:
        # a token that quietly expires because nothing ever refreshed it.
        store = TokenStore(storage)
        client = FakeClient()
        start = at(1)

        for hours in range(0, 365 * 24, 6):
            now = start + timedelta(hours=hours)
            info = store.load(BOOTSTRAP, ACCOUNT, now=now)
            info, _ = store.refresh_if_needed(info, client, now=now)
            assert info.days_remaining(now) > 0, f"token expired after {hours}h"

        assert client.calls >= 5  # refreshed roughly every 53 days

    def test_round_trip_preserves_timezone_awareness(self, storage):
        # A naive datetime would break every comparison in the scheduler.
        store = TokenStore(storage)
        store.save(store.load(BOOTSTRAP, ACCOUNT, now=at(1)))

        loaded = store.load(BOOTSTRAP, ACCOUNT, now=at(2))
        assert loaded.expires_at.tzinfo is not None
        assert loaded.seeded_at.tzinfo is not None


class TestLedger:
    def test_empty_when_absent(self, storage):
        assert Ledger(storage).entries() == []
        assert Ledger(storage).last_published() is None

    def test_records_and_reads_back(self, storage):
        ledger = Ledger(storage)
        ledger.record("2026-09-20-run", media_id="media-1", post_type="carousel", now=at(20))

        fresh = Ledger(storage)
        assert fresh.is_published("2026-09-20-run")
        assert fresh.last_published() == at(20)
        assert fresh.find("2026-09-20-run").media_id == "media-1"

    def test_corrupt_ledger_raises_rather_than_reading_empty(self, storage):
        # Treating this as empty would republish the user's entire history.
        storage.write("state/published.json", b"{ truncated mid-w")

        with pytest.raises(LedgerCorrupt, match="repost your entire history"):
            Ledger(storage).entries()

    def test_last_published_can_exclude_immediate_posts(self, storage):
        ledger = Ledger(storage)
        ledger.record("scheduled", now=at(18))
        ledger.record("urgent", immediate=True, now=at(20))

        assert ledger.last_published() == at(20)
        assert ledger.last_published(scheduled_only=True) == at(18)

    def test_recovered_posts_are_marked(self, storage):
        # A crash-window recovery knows it published but not the media id.
        ledger = Ledger(storage)
        ledger.record("post", media_id=None, recovered=True, now=at(20))

        assert ledger.find("post").recovered
        assert ledger.find("post").media_id is None

    def test_timestamps_survive_as_aware(self, storage):
        Ledger(storage).record("post", now=at(20))
        assert Ledger(storage).entries()[0].published_at.tzinfo is not None

    def test_unknown_post_is_not_published(self, storage):
        assert not Ledger(storage).is_published("never-seen")


class TestEventLog:
    def test_creates_the_monthly_file(self, storage):
        EventLog(storage).emit(PUBLISHED, post="p", now=at(20))

        events = EventLog(storage).read(at(20))
        assert len(events) == 1
        assert events[0]["event"] == PUBLISHED
        assert events[0]["post"] == "p"

    def test_appends_without_losing_earlier_events(self, storage):
        log_ = EventLog(storage)
        log_.emit(PUBLISHED, post="a", now=at(20))
        log_.emit(PUBLISHED, post="b", now=at(21))

        assert [e["post"] for e in log_.read(at(20))] == ["a", "b"]

    def test_rolls_over_by_month(self, storage):
        log_ = EventLog(storage)
        log_.emit(PUBLISHED, post="september", now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        log_.emit(PUBLISHED, post="october", now=datetime(2026, 10, 1, tzinfo=timezone.utc))

        assert len(log_.read(datetime(2026, 9, 30, tzinfo=timezone.utc))) == 1
        assert len(log_.read(datetime(2026, 10, 1, tzinfo=timezone.utc))) == 1

    def test_each_line_is_valid_json(self, storage):
        log_ = EventLog(storage)
        log_.emit(PUBLISHED, post="a", now=at(20))
        log_.emit(TOKEN_REFRESHED, days_remaining=60, now=at(20))

        raw = storage.read(log_.path_for(at(20))).decode()
        for line in raw.strip().splitlines():
            json.loads(line)  # must not raise

    def test_never_records_the_token(self, storage):
        # The log is far more likely to be shared than the token file.
        log_ = EventLog(storage)
        log_.emit(TOKEN_REFRESHED, days_remaining=60, now=at(20))

        assert BOOTSTRAP not in storage.read(log_.path_for(at(20))).decode()

    def test_missing_month_reads_empty(self, storage):
        assert EventLog(storage).read(at(20)) == []

    def test_a_malformed_line_does_not_hide_the_month(self, storage):
        log_ = EventLog(storage)
        log_.emit(PUBLISHED, post="good", now=at(20))
        path = log_.path_for(at(20))
        storage.write(path, storage.read(path) + b"{ broken\n")
        log_.emit(PUBLISHED, post="also-good", now=at(21))

        assert len(log_.read(at(20))) == 2

    def test_write_failure_does_not_raise(self, storage):
        # Losing a log line must never lose a post that succeeded.
        class Broken(LocalStorage):
            def write(self, path, data):
                raise OSError("storage is down")

        log_ = EventLog(Broken(storage.root))
        log_.emit(PUBLISHED, post="p", now=at(20))  # must not raise


class TestPending:
    def test_absent_by_default(self, storage):
        assert PendingStore(storage).read("queue/post-a") is None

    def test_round_trip(self, storage):
        store = PendingStore(storage)
        store.write(
            "queue/post-a",
            "container-99",
            post_type="carousel",
            staged=["staging/post-a/01.jpg"],
            now=at(20),
        )

        pending = store.read("queue/post-a")
        assert pending.container_id == "container-99"
        assert pending.post_type == "carousel"
        assert pending.staged == ["staging/post-a/01.jpg"]
        assert pending.created_at.tzinfo is not None

    def test_lives_in_the_post_folder(self, storage):
        PendingStore(storage).write("queue/post-a", "c1", now=at(20))
        # So it travels with the post when the folder moves.
        assert storage.read("queue/post-a/publishing.json")

    def test_clear_removes_it(self, storage):
        store = PendingStore(storage)
        store.write("queue/post-a", "c1", now=at(20))
        store.clear("queue/post-a")

        assert store.read("queue/post-a") is None

    def test_unreadable_marker_is_ignored(self, storage):
        storage.write("queue/post-a/publishing.json", b"{ half written")
        assert PendingStore(storage).read("queue/post-a") is None

    def test_is_not_treated_as_post_media(self, storage):
        from autogram.content.media import is_reserved

        assert is_reserved("queue/post-a/publishing.json")
