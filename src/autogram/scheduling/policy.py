"""Parsing the posting policy from ``autogram.yml``.

The policy lives with the user's content rather than in the repository, so it
can be edited from a phone alongside the posts themselves.

    timezone: Europe/Lisbon

    schedule:
      cadence: every 2 days      # or: daily | monday,thursday
      window: "06:00-21:00"

Everything is optional; the defaults post daily between 09:00 and 21:00 UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"
DEFAULT_WINDOW = "09:00-21:00"
DEFAULT_CADENCE = "daily"

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_EVERY_N_DAYS = re.compile(r"^every\s+(\d+)\s+days?$", re.IGNORECASE)
_WINDOW = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


class PolicyError(ValueError):
    """The policy could not be understood. The message is for the user."""


@dataclass(frozen=True)
class Cadence:
    """How often to post.

    Exactly one of ``interval_days`` or ``weekdays`` is set. ``daily`` is
    represented as an interval of one day.
    """

    interval_days: int | None = None
    weekdays: frozenset[int] | None = None

    def describe(self) -> str:
        if self.weekdays is not None:
            names = [WEEKDAY_NAMES[d] for d in sorted(self.weekdays)]
            return " and ".join(names) if len(names) < 3 else ", ".join(names)
        if self.interval_days == 1:
            return "daily"
        return f"every {self.interval_days} days"


@dataclass(frozen=True)
class Window:
    """The span of a local day a post may land in."""

    start: time
    end: time

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (
            self.start.hour * 60 + self.start.minute
        )

    def describe(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class Policy:
    cadence: Cadence
    window: Window
    timezone: ZoneInfo
    timezone_name: str

    def describe(self) -> str:
        return (
            f"{self.cadence.describe()}, between {self.window.describe()} "
            f"{self.timezone_name}"
        )


def parse_cadence(raw) -> Cadence:
    if raw is None:
        raw = DEFAULT_CADENCE

    text = str(raw).strip().lower()
    if not text:
        raise PolicyError("cadence is empty.")

    if text == "daily":
        return Cadence(interval_days=1)

    match = _EVERY_N_DAYS.match(text)
    if match:
        days = int(match.group(1))
        if days < 1:
            raise PolicyError(f"cadence '{raw}' must be at least 1 day.")
        return Cadence(interval_days=days)

    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts and all(p in WEEKDAYS for p in parts):
        return Cadence(weekdays=frozenset(WEEKDAYS[p] for p in parts))

    raise PolicyError(
        f"cadence '{raw}' is not understood. Use 'daily', 'every N days', "
        f"or a list of weekdays such as 'monday,thursday'."
    )


def parse_window(raw) -> Window:
    if raw is None:
        raw = DEFAULT_WINDOW

    match = _WINDOW.match(str(raw).strip())
    if not match:
        raise PolicyError(
            f"window '{raw}' is not understood. Use HH:MM-HH:MM, "
            f'for example "06:00-21:00".'
        )

    start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
    for hour, minute in ((start_h, start_m), (end_h, end_m)):
        if hour > 23 or minute > 59:
            raise PolicyError(f"window '{raw}' contains an invalid time.")

    start, end = time(start_h, start_m), time(end_h, end_m)
    if start >= end:
        # Overnight windows would make "which day is this slot on?" ambiguous.
        raise PolicyError(
            f"window '{raw}' must start before it ends. Windows spanning "
            f"midnight are not supported."
        )

    return Window(start=start, end=end)


def parse_timezone(raw) -> tuple[ZoneInfo, str]:
    name = str(raw or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PolicyError(
            f"timezone '{name}' is not recognised. Use a name like "
            f"'Europe/Lisbon' or 'America/New_York'."
        ) from exc


def parse(config: dict | None) -> Policy:
    """Build a Policy from parsed ``autogram.yml``, filling in defaults."""
    config = config or {}
    if not isinstance(config, dict):
        raise PolicyError("autogram.yml must contain key: value settings.")

    schedule = config.get("schedule") or {}
    if not isinstance(schedule, dict):
        raise PolicyError("the 'schedule' setting must contain key: value settings.")

    zone, zone_name = parse_timezone(config.get("timezone"))
    return Policy(
        cadence=parse_cadence(schedule.get("cadence")),
        window=parse_window(schedule.get("window")),
        timezone=zone,
        timezone_name=zone_name,
    )
