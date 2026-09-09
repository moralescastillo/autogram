"""The pipeline, end to end.

Real storage, real content parsing, real validation, real scheduling — only
Instagram's HTTP is faked. These are the tests that exercise the orderings the
individual modules cannot check on their own: that a marker is written before a
long wait, that recovery precedes scheduling, that a dry run touches nothing.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from autogram.instagram.client import InstagramClient
from autogram.pipeline.context import Context
from autogram.pipeline.publish import Result, publish_post
from autogram.pipeline.recover import resolve_pending
from autogram.scheduling.policy import parse as parse_policy
from autogram.state.ledger import Ledger
from autogram.state.log import EventLog
from autogram.state.pending import PendingStore
from tests.conftest import FakeResponse, FakeSession

NOW = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
ACCOUNT = "17841400000000000"


def image_bytes(width=1080, height=1080, fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (100, 120, 140)).save(buffer, format=fmt)
    return buffer.getvalue()


def make_context(authoring, serving, responses, *, now=NOW, dry_run=False) -> Context:
    session = FakeSession(responses)
    client = InstagramClient(
        "TOKEN", ACCOUNT, session=session, sleep=lambda _: None, clock=lambda: 0
    )
    ctx = Context(
        authoring=authoring,
        serving=serving,
        client=client,
        ledger=Ledger(authoring),
        pending=PendingStore(authoring),
        events=EventLog(authoring),
        policy=parse_policy({"timezone": "UTC", "schedule": {"window": "06:00-21:00"}}),
        ig_user_id=ACCOUNT,
        now=now,
        dry_run=dry_run,
    )
    ctx.session = session
    return ctx


def add_post(storage, name, files, post_md=None, root="queue"):
    for filename, data in files.items():
        storage.write(f"{root}/{name}/{filename}", data)
    if post_md:
        storage.write(f"{root}/{name}/post.md", post_md.encode())


def get_post(ctx, name="post-a", root="queue"):
    from autogram.content.post import discover

    return next(p for p in discover(ctx.authoring, root) if p.name == name)


class TestPublishSingle:
    def test_happy_path(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()}, "---\ntype: single\n---\nHello")
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "container-1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "media-99"}),
            ],
        )

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.PUBLISHED
        assert outcome.media_id == "media-99"

    def test_records_moves_and_tidies_up(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "container-1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "media-99"}),
            ],
        )

        publish_post(ctx, get_post(ctx))

        assert Ledger(authoring).is_published("post-a")
        assert authoring.read("published/post-a/01.jpg")       # retired
        assert authoring.list("queue/post-a") == []            # gone from queue
        assert serving.list("staging/post-a") == []            # staged media removed
        assert PendingStore(authoring).read("published/post-a") is None

    def test_caption_reaches_instagram(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()}, "---\ntype: single\n---\nMy caption")
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "c1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "m1"}),
            ],
        )

        publish_post(ctx, get_post(ctx))

        assert ctx.session.calls[0]["data"]["caption"] == "My caption"

    def test_png_is_staged_as_jpeg(self, authoring, serving):
        add_post(authoring, "post-a", {"01.png": image_bytes(fmt="PNG")})
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "c1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "m1"}),
            ],
        )

        publish_post(ctx, get_post(ctx))

        assert ".jpg" in ctx.session.calls[0]["data"]["image_url"]


class TestPublishCarousel:
    def test_children_are_created_then_the_parent(self, authoring, serving):
        add_post(
            authoring,
            "post-a",
            {"01.jpg": image_bytes(), "02.jpg": image_bytes()},
            "---\ntype: carousel\n---\nTwo photos",
        )
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "child-1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "child-2"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "parent-1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "media-99"}),
            ],
        )

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.PUBLISHED
        parent_call = ctx.session.calls[4]
        assert parent_call["data"]["media_type"] == "CAROUSEL"
        assert parent_call["data"]["children"] == "child-1,child-2"

    def test_order_follows_natural_filename_sort(self, authoring, serving):
        add_post(
            authoring,
            "post-a",
            {"1.jpg": image_bytes(), "2.jpg": image_bytes(), "10.jpg": image_bytes()},
        )
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"id": f"child-{i}"}) if i % 2 == 0 else FakeResponse({"status_code": "FINISHED"})
             for i in range(6)]
            + [
                FakeResponse({"id": "parent"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "media"}),
            ],
        )

        publish_post(ctx, get_post(ctx))

        staged_urls = [c["data"].get("image_url", "") for c in ctx.session.calls if "data" in c]
        ordered = [u for u in staged_urls if u]
        assert "/1.jpg" in ordered[0]
        assert "/2.jpg" in ordered[1]
        assert "/10.jpg" in ordered[2]


class TestValidationFailure:
    def test_writes_error_txt_and_leaves_the_post(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes(1080, 2400)})  # too tall
        ctx = make_context(authoring, serving, [])

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.FAILED
        error = authoring.read("queue/post-a/error.txt").decode()
        assert "post-a" in error
        assert "4:5" in error
        # The post stays where it is, so fixing it is enough.
        assert authoring.read("queue/post-a/01.jpg")
        assert not Ledger(authoring).is_published("post-a")

    def test_nothing_is_uploaded(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes(1080, 2400)})
        ctx = make_context(authoring, serving, [])

        publish_post(ctx, get_post(ctx))

        # Validation runs before staging, so the bucket is untouched.
        assert serving.list("staging") == []
        assert ctx.session.calls == []

    def test_error_txt_is_removed_once_fixed(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        authoring.write("queue/post-a/error.txt", b"an earlier failure")

        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "c1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "m1"}),
            ],
        )
        publish_post(ctx, get_post(ctx))

        with pytest.raises(FileNotFoundError):
            authoring.read("published/post-a/error.txt")

    def test_duplicate_name_is_refused(self, authoring, serving):
        Ledger(authoring).record("post-a", media_id="old", now=NOW - timedelta(days=5))
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, [])

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.FAILED
        assert "already published" in authoring.read("queue/post-a/error.txt").decode()


class TestApiFailures:
    def test_media_rejection_fails_the_post(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse(
                    {"error": {"message": "Media could not be fetched", "code": 100,
                               "error_subcode": 2207003}},
                    status_code=400,
                )
            ],
        )

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.FAILED
        assert "could not be fetched" in authoring.read("queue/post-a/error.txt").decode()
        assert serving.list("staging/post-a") == []  # staged media cleaned up

    def test_rate_limit_leaves_no_error_txt(self, authoring, serving):
        # Nothing is wrong with the user's content; telling them so is noise.
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"error": {"message": "rate limited", "code": 4}}, status_code=400)],
        )

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.RETRY_LATER
        with pytest.raises(FileNotFoundError):
            authoring.read("queue/post-a/error.txt")

    def test_slow_video_is_retried_not_failed(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"id": "c1"})] + [FakeResponse({"status_code": "IN_PROGRESS"})] * 400,
        )
        # A clock that leaps past the deadline: the container never finishes.
        ticks = iter([0, 0] + [10_000] * 500)
        ctx.client._clock = lambda: next(ticks)

        outcome = publish_post(ctx, get_post(ctx))

        assert outcome.result is Result.RETRY_LATER
        with pytest.raises(FileNotFoundError):
            authoring.read("queue/post-a/error.txt")


class TestCrashWindow:
    def test_marker_is_written_before_the_wait(self, authoring, serving):
        # The wait can run 15 minutes — exactly where a runner timeout lands.
        # If the marker were written after it, a crash would publish twice.
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})

        seen = {}

        ctx = make_context(
            authoring,
            serving,
            [
                FakeResponse({"id": "container-1"}),
                FakeResponse({"status_code": "FINISHED"}),
                FakeResponse({"id": "media-99"}),
            ],
        )

        original_wait = ctx.client.wait_until_ready

        def spy(container_id, **kwargs):
            seen["marker"] = PendingStore(authoring).read("queue/post-a")
            return original_wait(container_id, **kwargs)

        ctx.client.wait_until_ready = spy
        publish_post(ctx, get_post(ctx))

        assert seen["marker"] is not None
        assert seen["marker"].container_id == "container-1"


class TestRecovery:
    def _pending_post(self, authoring, container="container-1", staged=None):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        PendingStore(authoring).write(
            "queue/post-a", container, post_type="single",
            staged=staged or [], now=NOW,
        )

    def test_already_published_is_recorded_not_republished(self, authoring, serving):
        self._pending_post(authoring)
        ctx = make_context(authoring, serving, [FakeResponse({"status_code": "PUBLISHED"})])

        outcome = resolve_pending(ctx, get_post(ctx))

        assert outcome.result is Result.PUBLISHED
        entry = Ledger(authoring).find("post-a")
        assert entry.recovered
        assert entry.media_id is None  # not recoverable from a container
        assert len(ctx.session.calls) == 1  # asked, did not publish again

    def test_finished_container_is_published(self, authoring, serving):
        self._pending_post(authoring)
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"status_code": "FINISHED"}), FakeResponse({"id": "media-99"})],
        )

        outcome = resolve_pending(ctx, get_post(ctx))

        assert outcome.result is Result.PUBLISHED
        assert Ledger(authoring).find("post-a").media_id == "media-99"

    def test_expired_container_is_discarded_for_a_fresh_attempt(self, authoring, serving):
        self._pending_post(authoring, staged=["staging/post-a/01.jpg"])
        serving.write("staging/post-a/01.jpg", image_bytes())
        ctx = make_context(authoring, serving, [FakeResponse({"status_code": "EXPIRED"})])

        outcome = resolve_pending(ctx, get_post(ctx))

        assert outcome is None  # falls through to a fresh attempt
        assert PendingStore(authoring).read("queue/post-a") is None
        assert serving.list("staging/post-a") == []

    def test_unknown_status_waits_rather_than_risking_a_duplicate(self, authoring, serving):
        self._pending_post(authoring)
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"error": {"message": "down", "code": 4}}, status_code=400)],
        )

        outcome = resolve_pending(ctx, get_post(ctx))

        assert outcome.result is Result.RETRY_LATER
        assert PendingStore(authoring).read("queue/post-a") is not None

    def test_no_marker_means_nothing_to_recover(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, [])

        assert resolve_pending(ctx, get_post(ctx)) is None


class TestStagingSweep:
    def test_orphaned_staging_is_removed(self, authoring, serving):
        from autogram.pipeline.publish import sweep_staging

        serving.write("staging/long-gone/01.jpg", b"x")
        serving.write("staging/post-a/01.jpg", b"x")
        ctx = make_context(authoring, serving, [])

        sweep_staging(ctx, {"post-a"})

        assert serving.list("staging/long-gone") == []
        assert serving.list("staging/post-a") != []
