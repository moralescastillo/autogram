"""Aspect ratios, and the YAML trap they hide.

PyYAML implements YAML 1.1, which reads ``1:1`` as a **sexagesimal integer** —
base 60, as in clock times. So this::

    aspect: 1:1

does not parse as the string ``"1:1"``. It parses as ``61``. Likewise ``4:5``
becomes 245, ``16:9`` becomes 969 and ``9:16`` becomes 556. Ratios containing a
decimal point, like ``1.91:1``, survive as strings because the dot breaks the
pattern.

Users will write these unquoted — the design's own example does — so both forms
have to work. The recovery is exact: ``divmod(n, 60)`` inverts the encoding.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Instagram crops to the first carousel item, so near-identical ratios must
#: compare equal. 1080x1081 is square as far as anyone is concerned.
RATIO_TOLERANCE = 0.01

# Feed posts must sit between 4:5 (portrait) and 1.91:1 (landscape).
MIN_FEED_RATIO = 4 / 5
MAX_FEED_RATIO = 1.91

# Stories and reels are full-screen vertical.
STORY_RATIO = 9 / 16


@dataclass(frozen=True)
class Aspect:
    width: float
    height: float

    @property
    def value(self) -> float:
        return self.width / self.height

    def matches(self, other: "Aspect | float", tolerance: float = RATIO_TOLERANCE) -> bool:
        target = other.value if isinstance(other, Aspect) else other
        if target == 0:
            return False
        return abs(self.value - target) / target <= tolerance

    def __str__(self) -> str:
        for label, ratio in (("1:1", 1.0), ("4:5", 0.8), ("16:9", 16 / 9), ("9:16", 9 / 16)):
            if self.matches(ratio, tolerance=0.005):
                return label
        return f"{self.value:.2f}:1"


class AspectError(ValueError):
    """An aspect ratio could not be understood."""


def parse(raw) -> Aspect:
    """Read an aspect ratio from whatever YAML produced.

    Accepts the string form (``"1:1"``, ``1.91:1``) and the integer PyYAML
    turns unquoted ratios into.
    """
    if isinstance(raw, Aspect):
        return raw

    if isinstance(raw, bool):  # bool is an int; nobody means True here
        raise AspectError(f"Invalid aspect ratio: {raw!r}")

    if isinstance(raw, int):
        # PyYAML read this as base-60. Undo it.
        width, height = divmod(raw, 60)
        if width <= 0 or height <= 0:
            raise AspectError(
                f"Invalid aspect ratio: {raw!r}. Write it as width:height, "
                f'for example 1:1 or "4:5".'
            )
        return Aspect(float(width), float(height))

    if isinstance(raw, float):
        raise AspectError(
            f"Invalid aspect ratio: {raw!r}. Write it as width:height, "
            f'for example 1:1 or "4:5".'
        )

    text = str(raw).strip()
    if ":" not in text:
        raise AspectError(
            f"Invalid aspect ratio: {raw!r}. Write it as width:height, "
            f'for example 1:1 or "4:5".'
        )

    left, _, right = text.partition(":")
    try:
        width, height = float(left), float(right)
    except ValueError:
        raise AspectError(f"Invalid aspect ratio: {raw!r}.") from None

    if width <= 0 or height <= 0:
        raise AspectError(f"Invalid aspect ratio: {raw!r}. Both sides must be positive.")

    return Aspect(width, height)


def of(width: int, height: int) -> Aspect:
    """The aspect ratio of an actual image or video."""
    return Aspect(float(width), float(height))
