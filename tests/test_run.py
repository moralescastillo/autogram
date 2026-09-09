"""The orchestrator: ordering, exit codes, and the dry-run contract."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from autogram.pipeline.context import Context
from autogram.run import (
    EXIT_OK,
    EXIT_POST_FAILED,
    _run_with_context,
    load_policy,
)
from autogram.scheduling.scheduler import target_time
from autogram.state.ledger import Ledger
from tests.test_pipeline import ACCOUNT, add_post, image_bytes, make_context

from tests.conftest import FakeResponse


def publish_responses():
    return [
        FakeResponse({"id": "container-1"}),
        FakeResponse({"status_code": "FINISHED"}),
        FakeResponse({"id": "media-99"}),
    ]


def due_now(ctx) -> datetime:
    """A moment just past today's target, so a post is due."""
    target = target_time(ctx.policy, ctx.now.date(), ACCOUNT)
    return target + timedelta(minutes=1)


def snapshot(storage, roots=("queue", "now", "published", "state", "staging")):
    listing = {}
    for root in roots:
        for entry in storage.list(root):
            listing[entry.path] = entry.is_dir
            if entry.is_dir:
                for child in storage.list(entry.path):
                    listing[child.path] = child.is_dir
    return listing


class TestPolicyLoading:
    def test_missing_file_uses_defaults(self, authoring):
        policy = load_policy(authoring)
        assert policy.cadence.interval_days == 1
        assert policy.timezone_name == "UTC"

    def test_reads_the_users_policy(self, authoring):
        authoring.write(
            "autogram.yml",
            b"timezone: Europe/Lisbon\nschedule:\n  cadence: every 2 days\n  window: \"08:00-20:00\"\n",
        )
        policy = load_policy(authoring)

        assert policy.cadence.interval_days == 2
        assert policy.timezone_name == "Europe/Lisbon"
        assert policy.window.describe() == "08:00-20:00"


class TestScheduledRun:
    def test_publishes_when_due(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, publish_responses())
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert Ledger(authoring).is_published("post-a")

    def test_does_nothing_before_the_slot(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, [])
        ctx.now = target_time(ctx.policy, ctx.now.date(), ACCOUNT) - timedelta(hours=1)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert not Ledger(authoring).is_published("post-a")
        assert ctx.session.calls == []

    def test_does_not_publish_twice_in_a_day(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, [])
        ctx.now = due_now(ctx)
        Ledger(authoring).record("earlier", now=ctx.now - timedelta(hours=2))

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert not Ledger(authoring).is_published("post-a")

    def test_empty_queue_when_due_is_not_an_error(self, authoring, serving):
        ctx = make_context(authoring, serving, [])
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK

    def test_publishes_only_one_post_per_run(self, authoring, serving):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        add_post(authoring, "post-b", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, publish_responses())
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=False)

        ledger = Ledger(authoring)
        assert ledger.is_published("post-a")
        assert not ledger.is_published("post-b")


class TestImmediatePosts:
    def test_now_folder_ignores_the_schedule(self, authoring, serving):
        add_post(authoring, "urgent", {"01.jpg": image_bytes()}, root="now")
        ctx = make_context(authoring, serving, publish_responses())
        # Deliberately before today's slot: cadence must not apply.
        ctx.now = target_time(ctx.policy, ctx.now.date(), ACCOUNT) - timedelta(hours=3)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert Ledger(authoring).is_published("urgent")

    def test_immediate_post_does_not_consume_the_scheduled_slot(self, authoring, serving):
        # An immediate post is outside the cadence by definition, so today's
        # scheduled post should still go out.
        add_post(authoring, "queued", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, publish_responses())
        ctx.now = due_now(ctx)
        Ledger(authoring).record("urgent", immediate=True, now=ctx.now - timedelta(hours=1))

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert Ledger(authoring).is_published("queued")

    def test_now_takes_priority_over_the_queue(self, authoring, serving):
        add_post(authoring, "queued", {"01.jpg": image_bytes()})
        add_post(authoring, "urgent", {"01.jpg": image_bytes()}, root="now")
        ctx = make_context(authoring, serving, publish_responses())
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=False)

        ledger = Ledger(authoring)
        assert ledger.is_published("urgent")
        assert not ledger.is_published("queued")


class TestRecoveryOrdering:
    def test_recovery_runs_before_scheduling(self, authoring, serving):
        # Recovering a published post writes the ledger, which is what tells
        # the scheduler the slot is used. Getting this backwards would post
        # twice in one day.
        from autogram.state.pending import PendingStore

        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        add_post(authoring, "post-b", {"01.jpg": image_bytes()})
        PendingStore(authoring).write("queue/post-a", "container-1", now=datetime.now(timezone.utc))

        ctx = make_context(authoring, serving, [FakeResponse({"status_code": "PUBLISHED"})])
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK

        ledger = Ledger(authoring)
        assert ledger.find("post-a").recovered
        assert not ledger.is_published("post-b")  # the slot is now used


class TestExitCodes:
    def test_failed_post_exits_non_zero(self, authoring, serving):
        # A non-zero exit is what makes GitHub email the user.
        add_post(authoring, "post-a", {"01.jpg": image_bytes(1080, 2400)})
        ctx = make_context(authoring, serving, [])
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_POST_FAILED

    def test_retry_later_exits_zero(self, authoring, serving):
        # Otherwise every slow video would email the user.
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(
            authoring,
            serving,
            [FakeResponse({"error": {"message": "rate limited", "code": 4}}, status_code=400)],
        )
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK


class TestDryRun:
    def test_changes_nothing_anywhere(self, authoring, serving, capsys):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()})
        ctx = make_context(authoring, serving, [FakeResponse({"id": "1", "username": "me"})])
        ctx.now = due_now(ctx)

        before_authoring = snapshot(authoring)
        before_serving = snapshot(serving)

        assert _run_with_context(ctx, dry_run=True) == EXIT_OK

        assert snapshot(authoring) == before_authoring
        assert snapshot(serving) == before_serving

    def test_reports_what_would_happen(self, authoring, serving, capsys):
        add_post(authoring, "post-a", {"01.jpg": image_bytes()}, "---\ntype: single\n---\nHi")
        ctx = make_context(authoring, serving, [FakeResponse({"id": "1", "username": "me"})])
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=True)
        output = capsys.readouterr().out

        assert "post-a" in output
        assert "Next slot" in output
        assert "all checks passed" in output
        assert "nothing was published" in output

    def test_surfaces_validation_problems(self, authoring, serving, capsys):
        # The point of a dry run: find the broken aspect ratio before the slot.
        add_post(authoring, "post-a", {"01.jpg": image_bytes(1080, 2400)})
        ctx = make_context(authoring, serving, [FakeResponse({"id": "1", "username": "me"})])
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=True)

        assert "4:5" in capsys.readouterr().out

    def test_never_writes_error_txt(self, authoring, serving, capsys):
        add_post(authoring, "post-a", {"01.jpg": image_bytes(1080, 2400)})
        ctx = make_context(authoring, serving, [FakeResponse({"id": "1", "username": "me"})])
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=True)

        with pytest.raises(FileNotFoundError):
            authoring.read("queue/post-a/error.txt")


class TestBrokenPostsDoNotStopTheRun:
    def test_bad_post_md_is_skipped(self, authoring, serving, capsys):
        add_post(authoring, "broken", {"01.jpg": image_bytes()}, "---\naspcet: 1:1\n---\n")
        ctx = make_context(authoring, serving, [FakeResponse({"id": "1", "username": "me"})])
        ctx.now = due_now(ctx)

        # Must not raise: one malformed folder cannot stop the whole run.
        assert _run_with_context(ctx, dry_run=True) == EXIT_OK

    def test_broken_post_does_not_block_the_one_behind_it(self, authoring, serving):
        # A single typo must not silently stop every later post from going out.
        add_post(authoring, "01-broken", {"01.jpg": image_bytes()}, "---\naspcet: 1:1\n---\n")
        add_post(authoring, "02-good", {"01.jpg": image_bytes()})

        ctx = make_context(authoring, serving, publish_responses())
        ctx.now = due_now(ctx)

        assert _run_with_context(ctx, dry_run=False) == EXIT_OK
        assert Ledger(authoring).is_published("02-good")

    def test_broken_post_gets_an_explanation(self, authoring, serving):
        add_post(authoring, "broken", {"01.jpg": image_bytes()}, "---\naspcet: 1:1\n---\n")
        ctx = make_context(authoring, serving, [])
        ctx.now = due_now(ctx)

        _run_with_context(ctx, dry_run=False)

        assert "aspcet" in authoring.read("queue/broken/error.txt").decode()
