"""Scheduling.

Pure logic with ``now`` injected, so every case here is exact rather than
approximate — including the ones that only happen twice a year or at the far
side of the world.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autogram.scheduling.policy import (
    Cadence,
    Policy,
    PolicyError,
    parse,
    parse_cadence,
    parse_timezone,
    parse_window,
)
from autogram.scheduling.scheduler import Decision, is_due, next_slot, target_time

ACCOUNT = "17841400000000000"
UTC = ZoneInfo("UTC")


def make_policy(cadence="daily", window="06:00-21:00", tz="Europe/Lisbon") -> Policy:
    return parse({"timezone": tz, "schedule": {"cadence": cadence, "window": window}})


def at(year, month, day, hour=12, minute=0, tz="Europe/Lisbon") -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


class TestPolicyParsing:
    def test_defaults_when_nothing_is_configured(self):
        policy = parse(None)
        assert policy.cadence.interval_days == 1
        assert policy.window.describe() == "09:00-21:00"
        assert policy.timezone_name == "UTC"

    @pytest.mark.parametrize(
        "raw,days",
        [("daily", 1), ("every 2 days", 2), ("EVERY 3 DAYS", 3), ("every 1 day", 1)],
    )
    def test_interval_cadences(self, raw, days):
        assert parse_cadence(raw).interval_days == days

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("monday", {0}),
            ("monday,thursday", {0, 3}),
            ("Mon, Thu", {0, 3}),
            ("sat,sun", {5, 6}),
        ],
    )
    def test_weekday_cadences(self, raw, expected):
        assert parse_cadence(raw).weekdays == expected

    @pytest.mark.parametrize(
        "raw", ["", "sometimes", "every 0 days", "every -1 days", "funday", "monday,funday"]
    )
    def test_invalid_cadences_are_rejected(self, raw):
        with pytest.raises(PolicyError):
            parse_cadence(raw)

    def test_window_parsing(self):
        window = parse_window("06:00-21:00")
        assert window.minutes == 15 * 60

    @pytest.mark.parametrize(
        "raw",
        ["21:00-06:00", "06:00", "25:00-26:00", "06:70-07:00", "nonsense", "12:00-12:00"],
    )
    def test_invalid_windows_are_rejected(self, raw):
        with pytest.raises(PolicyError):
            parse_window(raw)

    def test_overnight_window_says_why(self):
        with pytest.raises(PolicyError, match="spanning\nmidnight|midnight"):
            parse_window("22:00-02:00")

    def test_unknown_timezone_is_rejected(self):
        with pytest.raises(PolicyError, match="not recognised"):
            parse_timezone("Mars/Olympus_Mons")

    def test_cadence_descriptions_read_naturally(self):
        assert Cadence(interval_days=1).describe() == "daily"
        assert Cadence(interval_days=3).describe() == "every 3 days"
        assert Cadence(weekdays=frozenset({0, 3})).describe() == "Monday and Thursday"


class TestTargetTime:
    def test_is_stable_for_a_given_day(self):
        policy = make_policy()
        day = datetime(2026, 9, 20).date()
        targets = {target_time(policy, day, ACCOUNT) for _ in range(100)}
        assert len(targets) == 1

    def test_is_stable_across_processes(self):
        # hash() is randomised per process; SHA-256 is not. If this ever fails,
        # two runs on the same day would disagree about when to post.
        import subprocess
        import sys

        script = (
            "from autogram.scheduling.policy import parse;"
            "from autogram.scheduling.scheduler import target_time;"
            "from datetime import datetime;"
            "p = parse({'timezone':'UTC','schedule':{'window':'06:00-21:00'}});"
            "print(target_time(p, datetime(2026,9,20).date(), 'acct'))"
        )
        results = {
            subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, cwd="src"
            ).stdout.strip()
            for _ in range(3)
        }
        assert len(results) == 1

    def test_differs_between_days(self):
        policy = make_policy()
        targets = {
            target_time(policy, datetime(2026, 9, d).date(), ACCOUNT).time()
            for d in range(1, 29)
        }
        assert len(targets) > 20  # essentially always different

    def test_differs_between_accounts(self):
        # Two accounts on the same schedule should not post in lockstep.
        policy = make_policy()
        day = datetime(2026, 9, 20).date()
        assert target_time(policy, day, "account-a") != target_time(policy, day, "account-b")

    def test_always_lands_inside_the_window(self):
        policy = make_policy(window="06:00-21:00")
        for day in range(1, 366):
            target = target_time(policy, datetime(2026, 1, 1).date() + timedelta(days=day - 1), ACCOUNT)
            assert policy.window.start <= target.time() < policy.window.end

    def test_spreads_across_the_window(self):
        # A year of targets should use the whole window, not cluster.
        policy = make_policy(window="06:00-21:00")
        minutes = [
            target_time(policy, datetime(2026, 1, 1).date() + timedelta(days=d), ACCOUNT).hour * 60
            + target_time(policy, datetime(2026, 1, 1).date() + timedelta(days=d), ACCOUNT).minute
            for d in range(365)
        ]
        assert min(minutes) < 7 * 60      # some land early
        assert max(minutes) > 20 * 60     # some land late

    def test_narrow_window_still_works(self):
        policy = make_policy(window="12:00-12:30")
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)
        assert target.hour == 12 and target.minute < 30


class TestDueDaily:
    def test_due_once_the_target_has_passed(self):
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(policy, target + timedelta(minutes=1), ACCOUNT)
        assert decision.due

    def test_not_due_before_the_target(self):
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(policy, target - timedelta(minutes=1), ACCOUNT)
        assert not decision.due
        assert "not due yet" in decision.reason

    def test_a_late_run_still_publishes(self):
        # This is the whole point: GitHub's cron drifts, and an equality check
        # on the hour would miss the slot for the entire day.
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(policy, target + timedelta(hours=2), ACCOUNT)
        assert decision.due
        assert "late" in decision.reason

    def test_not_due_twice_in_one_day(self):
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(
            policy, target + timedelta(hours=1), ACCOUNT, last_published=target
        )
        assert not decision.due
        assert "already published today" in decision.reason

    def test_published_yesterday_is_due_again(self):
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(
            policy, target, ACCOUNT, last_published=target - timedelta(days=1)
        )
        assert decision.due


class TestDueEveryNDays:
    def test_first_post_is_due_immediately(self):
        # Waiting for an arbitrary epoch to come round would be baffling.
        policy = make_policy(cadence="every 3 days")
        decision = is_due(policy, at(2026, 9, 20, 23, 0), ACCOUNT, last_published=None)
        assert decision.due

    def test_not_due_before_the_interval_elapses(self):
        policy = make_policy(cadence="every 3 days")
        decision = is_due(
            policy, at(2026, 9, 20, 23, 0), ACCOUNT, last_published=at(2026, 9, 19, 12)
        )
        assert not decision.due
        assert "cadence is every 3 days" in decision.reason

    def test_due_once_the_interval_has_passed(self):
        policy = make_policy(cadence="every 3 days")
        decision = is_due(
            policy, at(2026, 9, 23, 23, 0), ACCOUNT, last_published=at(2026, 9, 20, 12)
        )
        assert decision.due

    def test_a_missed_day_does_not_shift_the_schedule_forever(self):
        # Counting from the last publish is self-healing: a skipped day means
        # the next one is due, not that the rhythm is permanently offset.
        policy = make_policy(cadence="every 2 days")
        decision = is_due(
            policy, at(2026, 9, 25, 23, 0), ACCOUNT, last_published=at(2026, 9, 20, 12)
        )
        assert decision.due


class TestDueWeekdays:
    def test_due_on_a_listed_day(self):
        policy = make_policy(cadence="monday,thursday")
        # 2026-09-24 is a Thursday.
        decision = is_due(policy, at(2026, 9, 24, 23, 0), ACCOUNT)
        assert decision.due

    def test_not_due_on_an_unlisted_day(self):
        policy = make_policy(cadence="monday,thursday")
        # 2026-09-25 is a Friday.
        decision = is_due(policy, at(2026, 9, 25, 23, 0), ACCOUNT)
        assert not decision.due
        assert "Friday is not a posting day" in decision.reason

    def test_weekday_cadence_ignores_elapsed_days(self):
        policy = make_policy(cadence="monday,thursday")
        decision = is_due(
            policy, at(2026, 9, 24, 23, 0), ACCOUNT, last_published=at(2026, 9, 23, 12)
        )
        assert decision.due


class TestTimezones:
    def test_local_date_decides_not_utc_date(self):
        # 15:30 UTC on the 20th is 00:30 on the 21st in Tokyo. Judging by the
        # UTC date would treat this as still the 20th — the day already posted
        # — and skip a post that is genuinely due.
        tokyo = ZoneInfo("Asia/Tokyo")
        policy = make_policy(cadence="daily", window="00:00-23:59", tz="Asia/Tokyo")
        now_utc = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)

        local = now_utc.astimezone(tokyo)
        assert local.date().day == 21

        # Ensure the day's slot has passed, so only the date logic is in play.
        target = target_time(policy, local.date(), ACCOUNT)
        now_utc = (target + timedelta(minutes=1)).astimezone(UTC)
        assert now_utc.astimezone(tokyo).date().day == 21

        decision = is_due(
            policy,
            now_utc,
            ACCOUNT,
            last_published=datetime(2026, 9, 20, 10, 0, tzinfo=tokyo),
        )
        # Published on the 20th Tokyo time; it is the 21st there now, so due.
        assert decision.due

    def test_already_published_today_respects_local_date(self):
        policy = make_policy(cadence="daily", window="00:00-23:59", tz="Asia/Tokyo")
        tokyo = ZoneInfo("Asia/Tokyo")

        decision = is_due(
            policy,
            datetime(2026, 9, 21, 15, 0, tzinfo=tokyo),
            ACCOUNT,
            last_published=datetime(2026, 9, 21, 9, 0, tzinfo=tokyo),
        )
        assert not decision.due

    @pytest.mark.parametrize("day", [(2026, 3, 29), (2026, 10, 25)])
    def test_dst_transitions_produce_valid_targets(self, day):
        # Europe/Lisbon springs forward and falls back on these dates.
        policy = make_policy(window="00:00-23:59")
        target = target_time(policy, datetime(*day).date(), ACCOUNT)

        assert target.tzinfo is not None
        assert target.astimezone(UTC).date() in (
            datetime(*day).date(),
            datetime(*day).date() + timedelta(days=1),
            datetime(*day).date() - timedelta(days=1),
        )

    def test_utc_run_compares_correctly_against_local_target(self):
        policy = make_policy(tz="America/New_York")
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        decision = is_due(policy, (target + timedelta(minutes=5)).astimezone(UTC), ACCOUNT)
        assert decision.due


class TestNextSlot:
    def test_reports_todays_slot_when_still_ahead(self):
        policy = make_policy()
        target = target_time(policy, datetime(2026, 9, 20).date(), ACCOUNT)

        assert next_slot(policy, target - timedelta(hours=1), ACCOUNT) == target

    def test_skips_to_the_next_eligible_weekday(self):
        policy = make_policy(cadence="monday")
        # 2026-09-22 is a Tuesday; the next Monday is the 28th.
        slot = next_slot(policy, at(2026, 9, 22, 12), ACCOUNT)
        assert slot.weekday() == 0
        assert slot.date().day == 28

    def test_respects_the_interval(self):
        policy = make_policy(cadence="every 3 days")
        slot = next_slot(
            policy, at(2026, 9, 21, 12), ACCOUNT, last_published=at(2026, 9, 20, 12)
        )
        assert slot.date() >= datetime(2026, 9, 23).date()

    def test_terminates_on_an_impossible_schedule(self):
        # A bounded loop rather than a hang, whatever the config says.
        policy = make_policy(cadence="every 900 days")
        assert next_slot(
            policy, at(2026, 9, 20, 12), ACCOUNT, last_published=at(2026, 9, 20, 12)
        ) is not None


class TestDecisionReporting:
    def test_decision_is_truthy(self):
        assert bool(Decision(True, "due"))
        assert not bool(Decision(False, "not due"))

    def test_every_decision_explains_itself(self):
        policy = make_policy(cadence="monday,thursday")
        for hour in range(0, 24, 3):
            decision = is_due(policy, at(2026, 9, 25, hour), ACCOUNT)
            assert decision.reason
            # A dry run should always be able to say when the next post lands.
            assert decision.target is not None
